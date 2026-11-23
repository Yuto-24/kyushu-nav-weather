"""Bounded, verifiable static snapshots for whole-site Cloudflare Pages deploys.

Only the current two releases are restored. status.json binds each slot pointer
by digest; each pointer binds its immutable release manifest and three assets.
Every remote read stays on the operator-supplied fixed HTTPS origin. No remote
write, Blob SDK, R2 bucket, credential, historical cache or numerical renderer
is involved in this module.

Pages-specific replacement rule: a valid terrain-partial MSM is preferred over
a same-day GSM fallback, and a strictly newer run may include terrain exclusions.
Source loss is rejected before those rules are evaluated.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from http.client import IncompleteRead
import json
import math
import os
from pathlib import Path
import re
import shutil
import ssl
import sys
import tempfile
from time import monotonic, sleep
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

try:
    from . import publication as common
except ImportError:
    import publication as common

JST, UTC = common.JST, common.UTC
SLOTS = ("0900", "1400")
STATUS_SCHEMA = "kyushu-pages-status-1"
POINTER_SCHEMA = "kyushu-daily-pointer-1"
MARKER = ".generated-kyushu-pages"
MAX_STATUS_BYTES = 32 * 1024
MAX_POINTER_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 512 * 1024
MAX_RENDER_MANIFEST_BYTES = 1024 * 1024
MAX_WEATHER_BYTES = 4 * 1024 * 1024
ASSET_LIMITS = {"png": 12 * 1024 * 1024, "pdf": 4 * 1024 * 1024,
                "weather_json": MAX_WEATHER_BYTES}
ASSET_MEDIA = {"png": "image/png", "pdf": "application/pdf", "weather_json": "application/json"}
MAX_FRONTEND_BYTES = 1024 * 1024
PUBLIC_FRONTEND = ("index.html", "app.js", "style.css", "config.json", "bootstrap/manifest.json")
LOCAL_FRONTEND = (*PUBLIC_FRONTEND, "404.html", "_headers")
MAX_SNAPSHOT_BYTES = 52 * 1024 * 1024
MAX_FETCHES = 19  # Two status reads, ten forecast files and seven local frontend files.
MAX_VERIFICATION_SECONDS = 300
MAX_VERIFICATION_REQUESTS = 96
MAX_VERIFICATION_BYTES = 128 * 1024 * 1024
RELEASE_ID = re.compile(r"\d{8}T\d{12}Z-[a-f0-9]{12}")
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,179}")
SHA256 = re.compile(r"[a-f0-9]{64}")
# The single insertion observed on this origin also matches NavMate's reviewed
# allowlist. Never learn or broaden this exception from a remote response.
HTML_END = b"</body>\n</html>\n"
CLOUDFLARE_ANALYTICS = (
    b'<script type="module" src="https://static.cloudflareinsights.com/beacon.min.js/'
    b'vce701823e78c4301a35b7e07294d87301792088814092" '
    b'integrity="sha512-UKNk8GbxkABxKcXYuLkO16EleLGUkcvJWtpSNmExE0Too3ZaFAxTscNWEc1OdAv5/'
    b'BikHawapSCaiD2XCmp+Bw==" '
    b'data-cf-beacon=\'{"version":"2024.11.0","token":"95f71c67cb5a41d3ab040fe18f7bcaa4",'
    b'"r":1,"spa":2}\' crossorigin="anonymous"></script>\n'
)


class PagesError(common.PublicationError):
    code = "pages_publication_failed"


class RestoreError(PagesError):
    code = "previous_publication_restore_failed"


class MissingRemote(RestoreError):
    code = "previous_publication_missing"


class PublicationChanged(common.InvalidPackage):
    """Mutable published files have not converged to one complete candidate."""


class DeploymentMismatch(PublicationChanged):
    """A well-formed status identifies a different deployment candidate."""


class TransientReadError(RestoreError):
    """A read-only HTTP request may succeed after a bounded retry."""


class VerificationTimeout(PagesError):
    """The complete readback exceeded its wall-clock budget."""


@dataclass
class Snapshot:
    status: dict
    pointers: dict[str, dict]
    files: dict[str, bytes]


def require(condition, message):
    if not condition:
        raise common.InvalidPackage(message)


def json_object(body: bytes) -> dict:
    value = common.json_object(body)
    try:
        common.json_bytes(value)  # Also rejects overflowed JSON float exponents.
    except (ValueError, TypeError) as exc:
        raise common.InvalidPackage("JSON contains invalid numeric values") from exc
    return value


def bounded_json(path: Path, limit: int = MAX_RENDER_MANIFEST_BYTES) -> dict:
    require(path.is_file() and path.stat().st_size <= limit, "JSON file is absent or exceeds its size bound")
    return json_object(path.read_bytes())


def origin_url(value: str) -> str:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise common.InvalidPackage("Invalid publication origin") from exc
    require(parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
            and port is None and parsed.path in ("", "/") and not parsed.query and not parsed.fragment,
            "Use a fixed HTTPS origin without a path, port or credentials")
    return urlunsplit(("https", parsed.hostname.lower(), "", "", ""))


def descriptor(path: str, body: bytes, media_type: str = "application/json") -> dict:
    return {"path": path, "bytes": len(body), "sha256": common.sha256(body), "media_type": media_type}


def validate_descriptor(record: dict, *, limit: int, expected_path: str | None = None,
                        media_type: str = "application/json") -> str:
    require(isinstance(record, dict), "Missing artifact descriptor")
    require(isinstance(record.get("path"), str), "Invalid artifact path type")
    path = common.safe_key(record.get("path", ""))
    require(not any(char in path for char in ("%", "?", "#", "\\")), "Invalid encoded artifact path")
    require(expected_path is None or path == expected_path, "Artifact path differs from its declared identity")
    require(isinstance(record.get("bytes"), int) and not isinstance(record["bytes"], bool)
            and 0 < record["bytes"] <= limit, "Artifact byte count exceeds its bound")
    require(isinstance(record.get("sha256"), str) and bool(SHA256.fullmatch(record["sha256"])),
            "Invalid artifact SHA-256")
    require(record.get("media_type") == media_type, "Artifact content type differs from its contract")
    return path


def checked_body(record: dict, body: bytes) -> bytes:
    require(len(body) == record["bytes"] and common.sha256(body) == record["sha256"],
            "Artifact byte count or SHA-256 mismatch")
    return body


def release_asset_path(record: dict, kind: str, prefix: str) -> str:
    path = validate_descriptor(record, limit=ASSET_LIMITS[kind], media_type=ASSET_MEDIA[kind])
    name = path.removeprefix(prefix)
    require(path.startswith(prefix) and FILE_NAME.fullmatch(name) and name != "manifest.json"
            and record.get("url") == "/" + path and record.get("download_url") == "/" + path,
            "Artifact path/URL escaped its immutable release")
    return path


def validate_pointer_paths(pointer: dict, slot: str) -> None:
    require(pointer.get("schema") == POINTER_SCHEMA and pointer.get("slot") == slot,
            "Invalid published slot pointer")
    day = common.day_string(pointer.get("valid_date"))
    release_id = pointer.get("release_id")
    require(isinstance(release_id, str) and bool(RELEASE_ID.fullmatch(release_id)), "Invalid release identity")
    prefix = f"releases/{day}/{slot}/{release_id}/"
    path = validate_descriptor(pointer.get("manifest", {}), limit=MAX_MANIFEST_BYTES,
                               expected_path=prefix + "manifest.json")
    require(pointer.get("manifest_url") == "/" + path, "Manifest URL escaped the fixed site")
    files = pointer.get("files")
    require(isinstance(files, dict) and set(files) == set(ASSET_LIMITS), "Invalid release artifact set")
    for kind, record in files.items():
        release_asset_path(record, kind, prefix)


def check_frontend(path: str, body: bytes, expected_hash: str, *, allow_analytics: bool) -> None:
    if common.sha256(body) == expected_hash:
        return
    suffix = CLOUDFLARE_ANALYTICS + HTML_END
    if (allow_analytics and path == "index.html" and body.endswith(suffix)
            and body.count(CLOUDFLARE_ANALYTICS) == 1):
        original = body[:-len(suffix)] + HTML_END
        if common.sha256(original) == expected_hash:
            return
    raise PublicationChanged(f"Published frontend differs from the candidate: {path}")


def local_asset(directory: Path, record: dict, kind: str) -> bytes:
    path = validate_descriptor(record, limit=ASSET_LIMITS[kind], media_type=ASSET_MEDIA[kind])
    require(FILE_NAME.fullmatch(path), "Local renderer artifacts must be plain filenames")
    source = (directory / path).resolve()
    require(source.parent == directory.resolve(), "Renderer artifact escaped its directory")
    require(source.is_file() and source.stat().st_size <= ASSET_LIMITS[kind], "Renderer artifact is absent or too large")
    return checked_body(record, source.read_bytes())


def validate_archive_package(manifest: dict, directory: Path, *, now: datetime) -> tuple[dict, dict]:
    """Validate prior/seed editions without rewriting their actual historical dates."""
    require(manifest.get("schema") == "kyushu-daily-publication-1" and
            manifest.get("timezone") == "Asia/Tokyo" and len(manifest.get("forecasts", [])) == 1,
            "A release manifest must contain exactly one forecast")
    entry = deepcopy(manifest["forecasts"][0])
    day = common.day_string(entry.get("valid_date"))
    slot = entry.get("slot")
    require(slot in SLOTS and manifest.get("target_date") == day, "Release date/slot mismatch")
    valid = common.timestamp(entry.get("valid_at_jst"))
    expected = datetime.combine(datetime.fromisoformat(day).date(), time(common.SLOTS[slot]), JST)
    require(valid == expected and entry.get("valid_time_jst") == f"{common.SLOTS[slot]:02d}:00",
            "Release valid time mismatch")
    initial = common.timestamp(entry.get("initial_time_jst"))
    generated = common.timestamp(entry.get("image_generated_time_jst"))
    acquired = common.timestamp(entry.get("weather_generated_time_jst"))
    now = common.timestamp(now)
    require(entry.get("model") in ("MSM", "GSM") and
            common.timestamp(entry.get("initial_time_utc")) == initial,
            "Release has no consistent actual source run")
    require(initial <= valid and initial <= acquired <= generated <= now + timedelta(seconds=5),
            "Release timestamps are inconsistent or in the future")
    require(day <= now.astimezone(JST).date().isoformat(), "A future target date cannot be seeded as a previous edition")
    assets = {}
    for kind in ("png", "pdf"):
        record = entry.get("files", {}).get(kind, {})
        assets[kind] = (record, local_asset(directory, record, kind))
    weather_record = manifest.get("forecast_json", {})
    assets["weather_json"] = (weather_record, local_asset(directory, weather_record, "weather_json"))
    weather = json_object(assets["weather_json"][1])
    require(weather.get("schema") == "pseudo-kyushu-paper-forecast-1" and len(weather.get("forecasts", [])) == 1,
            "Release weather JSON must contain one forecast")
    forecast = weather["forecasts"][0]
    metadata = forecast.get("metadata", {})
    require(metadata.get("target_date") == day and metadata.get("valid_time_jst") == entry["valid_time_jst"]
            and metadata.get("model") == entry["model"]
            and common.timestamp(metadata.get("initial_time_utc")) == initial
            and common.timestamp(metadata.get("generated_time_jst")) == acquired,
            "Actual weather metadata differs from the displayed release")
    require(not metadata.get("source_acquisition_failures", 0) and not metadata.get("source_missing_forecasts", 0),
            "Unresolved source loss cannot be published")
    deadline = datetime.combine(expected.date(), time(5 if slot == "0900" else 10), JST)
    if metadata.get("as_of_cutoff_utc") is not None:
        cutoff = common.timestamp(metadata["as_of_cutoff_utc"])
        require(initial <= cutoff <= min(acquired, deadline), "Weather acquisition cutoff is inconsistent")
    groups = forecast.get("groups", [])
    require(len(groups) == 10 and sorted(g.get("id", "") for g in groups) == list("ABCDEFGHIJ"),
            "All ten fixed sectors are required")
    counts = {"valid": 0, "terrain": 0, "partial": 0}
    for group in groups:
        rows = group.get("rows", [])
        require([row.get("altitude_ft_msl") for row in rows] == common.ALTITUDES,
                "All nine descending altitudes are required")
        for row in rows:
            state = row.get("status")
            require(state in ("valid", "terrain"), "Missing weather rows cannot replace a completed sheet")
            masks = row.get("mask_reason_counts", {})
            require(isinstance(masks, dict) and not row.get("source_acquisition_failures", 0)
                    and not row.get("source_nonfinite_count", 0)
                    and not masks.get("outside_vertical_range_count", 0)
                    and not any(value for key, value in masks.items() if key.startswith("source_")),
                    "A row contains source loss or unavailable vertical levels")
            terrain = sum(masks.get(key, 0) for key in
                          ("target_at_or_below_terrain_count", "lower_bracket_below_terrain_count"))
            if state == "terrain" or row.get("partial_flag"):
                require(terrain > 0, "A terrain-only exclusion must have an actual terrain reason")
            if state == "valid":
                require(all(isinstance(row.get(key), (int, float)) and not isinstance(row[key], bool)
                            and math.isfinite(row[key]) for key in common.NUMERIC_FIELDS)
                        and row.get("fuel_per_minute_us_gal_display"), "Incomplete numeric weather/performance row")
            counts[state] += 1
            counts["partial"] += bool(row.get("partial_flag"))
    coverage = entry.get("coverage", {})
    require(counts["valid"] > 0 and coverage.get("total_rows") == 90 and coverage.get("missing_rows") == 0
            and coverage.get("valid_weather_rows") == counts["valid"]
            and coverage.get("complete_numeric_rows") == counts["valid"]
            and coverage.get("terrain_rows") == counts["terrain"]
            and coverage.get("partial_rows") == counts["partial"]
            and coverage.get("all_nonterrain_rows_complete") is True,
            "Release coverage does not match its actual weather rows")
    entry["quality"] = "partial" if counts["partial"] else "complete"
    entry["publication_deadline_jst"] = deadline.isoformat()
    return entry, assets


def replacement_allowed(current: dict | None, candidate: dict) -> None:
    if (current and candidate["valid_date"] == current["valid_date"]
            and current["model"] == candidate["model"]
            and common.timestamp(candidate["weather_generated_time_jst"])
            < common.timestamp(current["weather_generated_time_jst"])):
        raise common.StalePublication("Older weather generation cannot replace the public sheet")
    if (current and candidate["valid_date"] == current["valid_date"]
            and current["model"] == "GSM" and candidate["model"] == "MSM"):
        return  # Source validity was checked; terrain exclusion is not acquisition failure.
    if (current and candidate["valid_date"] == current["valid_date"]
            and current["model"] == candidate["model"]
            and common.timestamp(candidate["initial_time_jst"]) > common.timestamp(current["initial_time_jst"])):
        # A new model run can legitimately change terrain-bracket coverage.
        # Keep date/model/generation ordering, but do not lock an older full
        # run merely because the newer one has legitimate terrain exclusions.
        common.replacement_allowed({**current, "quality": candidate["quality"]}, candidate)
        return
    common.replacement_allowed(current, candidate)


def renderer_packages(manifest_path: Path, workspace: Path) -> list[tuple[dict, Path]]:
    """Normalize a one/two-slot renderer batch into independently validated releases."""
    manifest_path = Path(manifest_path).resolve()
    manifest = bounded_json(manifest_path)
    require(manifest.get("schema") == "kyushu-daily-publication-1", "Unknown renderer manifest schema")
    forecasts = manifest.get("forecasts", [])
    require(isinstance(forecasts, list) and 1 <= len(forecasts) <= 2 and
            len({entry.get("slot") for entry in forecasts}) == len(forecasts), "Invalid renderer forecast set")
    original_weather = manifest.get("forecast_json", {})
    weather_body = local_asset(manifest_path.parent, original_weather, "weather_json")
    weather = json_object(weather_body)
    require(weather.get("schema") == "pseudo-kyushu-paper-forecast-1" and
            len(weather.get("forecasts", [])) == len(forecasts), "Renderer weather batch mismatch")
    result = []
    for original in forecasts:
        slot, day = original.get("slot"), common.day_string(original.get("valid_date"))
        require(slot in SLOTS and manifest.get("target_date") == day, "Renderer target date mismatch")
        matches = [f for f in weather["forecasts"] if
                   f.get("metadata", {}).get("target_date") == day and
                   f.get("metadata", {}).get("valid_time_jst") == original.get("valid_time_jst")]
        require(len(matches) == 1, "A renderer forecast has no unique source weather entry")
        directory = workspace / uuid.uuid4().hex
        directory.mkdir(parents=True)
        entry = deepcopy(original)
        for kind in ("png", "pdf"):
            record = entry.get("files", {}).get(kind, {})
            body = local_asset(manifest_path.parent, record, kind)
            (directory / record["path"]).write_bytes(body)
        split_weather = {**weather, "forecasts": matches}
        split_body = common.json_bytes(split_weather)
        weather_name = f"kyushu_pseudo_forecasts_{day.replace('-', '')}_{slot}.json"
        (directory / weather_name).write_bytes(split_body)
        package = {**deepcopy(manifest), "forecasts": [entry],
                   "forecast_json": descriptor(weather_name, split_body), "combined_pdf": None,
                   "source_renderer_manifest_sha256": common.sha256(manifest_path.read_bytes()),
                   "source_weather_batch_sha256": common.sha256(weather_body)}
        # Per-page layout checks remain a build input; they are not public dependencies.
        package.pop("layout_report", None)
        result.append((package, directory))
    return result


def make_release(manifest: dict, directory: Path, *, now: datetime,
                 historical_seed: bool = False) -> tuple[dict, dict[str, bytes]]:
    entry, assets = validate_archive_package(manifest, directory, now=now)
    if not historical_seed:
        common.validate_package(manifest, directory, entry["valid_date"], entry["slot"], now=now)
    generated = common.timestamp(entry["image_generated_time_jst"])
    fingerprint = common.sha256(common.json_bytes({"entry": entry,
                          "artifacts": {kind: record["sha256"] for kind, (record, _) in assets.items()}}))[:12]
    release_id = generated.strftime("%Y%m%dT%H%M%S%fZ") + "-" + fingerprint
    prefix = f"releases/{entry['valid_date']}/{entry['slot']}/{release_id}/"
    files, published = {}, {}
    for kind, (record, body) in assets.items():
        path = prefix + record["path"]
        files[path] = body
        published[kind] = {**record, "path": path, "url": "/" + path, "download_url": "/" + path}
    release_manifest = {**manifest, "release_id": release_id, "published_files": published}
    manifest_path = prefix + "manifest.json"
    manifest_body = common.json_bytes(release_manifest)
    require(len(manifest_body) <= MAX_MANIFEST_BYTES, "Release manifest exceeds its size bound")
    files[manifest_path] = manifest_body
    prepared_at = common.timestamp(now).astimezone(JST)
    pointer = {**entry, "schema": POINTER_SCHEMA, "release_id": release_id, "files": published,
               "manifest_url": "/" + manifest_path,
               "manifest": descriptor(manifest_path, manifest_body),
               "candidate_prepared_at_jst": prepared_at.isoformat(),
               "deadline_met": None}
    body = common.json_bytes(pointer)
    require(len(body) <= MAX_POINTER_BYTES, "Slot pointer exceeds its size bound")
    files[f"latest/{entry['slot']}.json"] = body
    return pointer, files


def status_for(pointers: dict[str, dict], files: dict[str, bytes], *, now: datetime,
               frontend_hashes: dict | None = None) -> dict:
    slots = {}
    for slot in SLOTS:
        value = pointers.get(slot)
        if value is None:
            slots[slot] = None
            continue
        pointer_path = f"latest/{slot}.json"
        slots[slot] = {"valid_date": value["valid_date"], "quality": value["quality"],
                       "model": value["model"], "initial_time_jst": value["initial_time_jst"],
                       "generated_at_jst": value["weather_generated_time_jst"],
                       "image_generated_at_jst": value["image_generated_time_jst"],
                       "release_id": value["release_id"],
                       "pointer": descriptor(pointer_path, files[pointer_path])}
    status = {"schema": STATUS_SCHEMA, "built_at_jst": common.timestamp(now).astimezone(JST).isoformat(),
              "slots": slots, "frontend_sha256": frontend_hashes or {}}
    status["deployment_id"] = "pages-" + common.sha256(common.json_bytes(status))[:24]
    return status


def validate_status(status: dict) -> None:
    require(status.get("schema") == STATUS_SCHEMA and isinstance(status.get("slots"), dict)
            and set(status["slots"]) == set(SLOTS), "Unknown Pages status schema")
    common.timestamp(status.get("built_at_jst"))
    require(isinstance(status.get("deployment_id"), str) and
            bool(re.fullmatch(r"pages-[a-f0-9]{24}", status["deployment_id"])), "Invalid deployment identity")
    identity = {key: value for key, value in status.items() if key != "deployment_id"}
    require(status["deployment_id"] == "pages-" + common.sha256(common.json_bytes(identity))[:24],
            "Deployment status identity mismatch")
    for slot, summary in status["slots"].items():
        if summary is None:
            continue
        require(isinstance(summary, dict), "Invalid status slot")
        common.day_string(summary.get("valid_date"))
        require(summary.get("quality") in ("complete", "partial") and summary.get("model") in ("MSM", "GSM"),
                "Invalid status source/quality")
        release_id = summary.get("release_id")
        require(isinstance(release_id, str) and bool(RELEASE_ID.fullmatch(release_id)), "Invalid release identity")
        initial, generated, image_generated = (common.timestamp(summary.get(key)) for key in
                                                ("initial_time_jst", "generated_at_jst", "image_generated_at_jst"))
        require(initial <= generated <= image_generated, "Invalid status timestamp order")
        validate_descriptor(summary.get("pointer", {}), limit=MAX_POINTER_BYTES,
                            expected_path=f"latest/{slot}.json")


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPReader:
    def __init__(self, origin: str, *, deadline: float | None = None):
        self.origin = origin_url(origin)
        self.opener = build_opener(_NoRedirects)
        self.nonce = uuid.uuid4().hex
        self.sequence = 0
        self.deadline = monotonic() + MAX_VERIFICATION_SECONDS if deadline is None else deadline

    def remaining(self) -> float:
        seconds = self.deadline - monotonic()
        if seconds <= 0:
            raise VerificationTimeout("Publication readback exceeded its 300-second budget")
        return min(20, seconds)

    def __call__(self, path: str, limit: int) -> bytes:
        common.safe_key(path)
        self.sequence += 1
        # Pages redirects /index.html to /; select the canonical path explicitly.
        request_path = "" if path == "index.html" else path
        request = Request(f"{self.origin}/{request_path}?_snapshot={self.nonce}-{self.sequence}",
                          headers={"Accept-Encoding": "identity", "Cache-Control": "no-cache",
                                   "User-Agent": "KyushuNavWeather-PagesPublication/1"})
        try:
            with self.opener.open(request, timeout=self.remaining()) as response:
                if response.status != 200:
                    raise RestoreError("Publication did not return HTTP 200")
                declared = response.headers.get("Content-Length")
                if declared is not None and (not declared.isdecimal() or int(declared) > limit):
                    raise RestoreError("Remote artifact exceeds its byte bound")
                chunks, received = [], 0
                while received <= limit:
                    self.remaining()
                    chunk = response.read1(min(64 * 1024, limit + 1 - received))
                    self.remaining()
                    if not chunk:
                        break
                    chunks.append(chunk)
                    received += len(chunk)
                body = b"".join(chunks)
                if len(body) > limit:
                    raise RestoreError("Remote artifact exceeds its byte bound")
                if declared is not None and len(body) != int(declared):
                    raise TransientReadError(f"Incomplete published response: {path}")
        except HTTPError as exc:
            if exc.code == 404:
                raise MissingRemote("Required published file returned HTTP 404") from exc
            if exc.code in (408, 429) or 500 <= exc.code < 600:
                raise TransientReadError(f"Publication read failed with HTTP {exc.code}: {path}") from exc
            raise RestoreError(f"Publication read failed with HTTP {exc.code}") from exc
        except ssl.SSLError as exc:
            raise RestoreError("Publication TLS verification failed") from exc
        except URLError as exc:
            if isinstance(exc.reason, ssl.SSLError):
                raise RestoreError("Publication TLS verification failed") from exc
            raise TransientReadError(f"Publication transport failed: {path}") from exc
        except (OSError, IncompleteRead) as exc:
            raise TransientReadError(f"Publication transport was interrupted: {path}") from exc
        if not body:
            raise TransientReadError(f"Remote artifact is empty: {path}")
        return body


def snapshot_from_reader(reader: Callable[[str, int], bytes], *, now: datetime | None = None,
                         expected_status: bytes | None = None, frontend_paths: tuple[str, ...] = (),
                         allow_analytics: bool = False) -> Snapshot:
    now = now or common.utc_now()
    total, calls = 0, 0

    def get(path, limit):
        nonlocal total, calls
        calls += 1
        require(calls <= MAX_FETCHES, "Publication exceeds its request-count bound")
        body = reader(path, limit)
        require(isinstance(body, bytes) and 0 < len(body) <= limit, "Artifact exceeded its read bound")
        total += len(body)
        require(total <= MAX_SNAPSHOT_BYTES, "Publication exceeds its aggregate byte bound")
        return body

    status_body = get("status.json", MAX_STATUS_BYTES)
    status = json_object(status_body)
    validate_status(status)
    if expected_status is not None and status_body != expected_status:
        expected_id = json_object(expected_status)["deployment_id"]
        raise DeploymentMismatch("Live deployment differs from the expected candidate "
                                 f"(expected {expected_id}, observed {status['deployment_id']})")
    files, pointers = {"status.json": status_body}, {}
    if frontend_paths:
        hashes = status.get("frontend_sha256")
        require(isinstance(hashes, dict) and set(hashes) == set(LOCAL_FRONTEND)
                and all(isinstance(value, str) and SHA256.fullmatch(value) for value in hashes.values()),
                "Invalid candidate frontend inventory")
        for path in frontend_paths:
            require(path in LOCAL_FRONTEND, "Unknown frontend artifact")
            body = get(path, MAX_FRONTEND_BYTES)
            check_frontend(path, body, hashes[path], allow_analytics=allow_analytics)
            files[path] = body
    with tempfile.TemporaryDirectory(prefix="kyushu-pages-verify-") as temporary:
        for slot in SLOTS:
            summary = status["slots"][slot]
            if summary is None:
                continue
            day, release_id = summary["valid_date"], summary["release_id"]
            prefix = f"releases/{day}/{slot}/{release_id}/"
            record = summary["pointer"]
            path = record["path"]
            pointer_body = get(path, MAX_POINTER_BYTES)
            pointer = json_object(pointer_body)
            validate_pointer_paths(pointer, slot)
            try:
                checked_body(record, pointer_body)
            except common.InvalidPackage as exc:
                raise PublicationChanged(f"Published pointer differs from its status: {path}") from exc
            require(pointer.get("schema") == POINTER_SCHEMA and pointer.get("slot") == slot
                    and pointer.get("release_id") == release_id, "Slot pointer identity mismatch")
            for status_key, pointer_key in (("valid_date", "valid_date"), ("quality", "quality"), ("model", "model"),
                                            ("initial_time_jst", "initial_time_jst"),
                                            ("generated_at_jst", "weather_generated_time_jst"),
                                            ("image_generated_at_jst", "image_generated_time_jst")):
                require(summary.get(status_key) == pointer.get(pointer_key), "Status and slot pointer disagree")
            common.timestamp(pointer.get("candidate_prepared_at_jst"))
            require(pointer.get("deadline_met") is None and "published_at_jst" not in pointer,
                    "A prepared candidate cannot claim an actual publication time")
            files[path] = pointer_body
            record = pointer.get("manifest", {})
            manifest_path = validate_descriptor(record, limit=MAX_MANIFEST_BYTES, expected_path=prefix + "manifest.json")
            require(pointer.get("manifest_url") == "/" + manifest_path, "Manifest URL escaped the fixed site")
            manifest_body = checked_body(record, get(manifest_path, record["bytes"]))
            manifest = json_object(manifest_body)
            require(manifest.get("release_id") == release_id and manifest.get("published_files") == pointer.get("files"),
                    "Release manifest and pointer artifacts disagree")
            files[manifest_path] = manifest_body
            public_files = pointer.get("files", {})
            require(set(public_files) == set(ASSET_LIMITS), "Release must contain PNG, PDF and actual weather JSON only")
            directory = Path(temporary) / slot
            directory.mkdir()
            for kind, record in public_files.items():
                artifact_path = release_asset_path(record, kind, prefix)
                name = artifact_path.removeprefix(prefix)
                body = checked_body(record, get(artifact_path, record["bytes"]))
                files[artifact_path] = body
                (directory / name).write_bytes(body)
            validated, assets = validate_archive_package(manifest, directory, now=now)
            for key, value in validated.items():
                if key != "files":
                    require(pointer.get(key) == value, f"Pointer field differs from its immutable manifest: {key}")
            for kind, (local, _) in assets.items():
                public = public_files[kind]
                require(public["path"] == prefix + local["path"] and
                        all(public[key] == local[key] for key in ("bytes", "sha256", "media_type")),
                        "Public artifact differs from renderer manifest")
            pointers[slot] = pointer
    final_status = get("status.json", MAX_STATUS_BYTES)
    if final_status != status_body:
        validate_status(json_object(final_status))
        raise PublicationChanged("Publication changed during readback; restart a complete snapshot")
    return Snapshot(status, pointers, files)


def local_snapshot(directory: Path, *, now: datetime | None = None, include_frontend: bool = False) -> Snapshot:
    directory = Path(directory).resolve()

    def reader(path, limit):
        source = (directory / common.safe_key(path)).resolve()
        require(source.is_relative_to(directory) and source.is_file() and source.stat().st_size <= limit,
                "A required local publication file is absent, outside the site or too large")
        return source.read_bytes()

    return snapshot_from_reader(reader, now=now, frontend_paths=LOCAL_FRONTEND if include_frontend else ())


def write_snapshot(directory: Path, files: dict[str, bytes]) -> None:
    for path, body in files.items():
        target = directory / common.safe_key(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    (directory / MARKER).write_text("Generated static publication; no credentials.\n", encoding="utf-8")


def commit_directory(staged: Path, destination: Path) -> None:
    destination = destination.resolve()
    if destination.exists():
        require(destination.is_dir() and (not any(destination.iterdir()) or (destination / MARKER).is_file()),
                "Refusing to replace an unrecognized output directory")
        backup = destination.with_name(destination.name + ".previous-" + uuid.uuid4().hex)
        os.replace(destination, backup)
        try:
            os.replace(staged, destination)
        except BaseException:
            os.replace(backup, destination)
            raise
        shutil.rmtree(backup)
    else:
        os.replace(staged, destination)


def restore_previous(origin: str, destination: Path, *, allow_empty: bool = False,
                     reader: Callable | None = None, now: datetime | None = None) -> dict:
    """Restore a complete prior snapshot; ordinary 404/network errors fail closed."""
    origin = origin_url(origin)
    now = now or common.utc_now()
    try:
        snapshot = snapshot_from_reader(reader or HTTPReader(origin), now=now)
    except MissingRemote:
        if not allow_empty:
            raise
        # Explicit initial-only exception: require that status itself is absent.
        source = reader or HTTPReader(origin)
        try:
            source("status.json", MAX_STATUS_BYTES)
        except MissingRemote:
            status = status_for({}, {}, now=now)
            snapshot = Snapshot(status, {}, {"status.json": common.json_bytes(status)})
        else:
            raise RestoreError("A referenced file is missing from an existing publication")
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".restore-pages-", dir=destination.parent) as temp:
        staging = Path(temp) / "site"
        staging.mkdir()
        write_snapshot(staging, snapshot.files)
        commit_directory(staging, destination)
    return {"directory": str(destination), "origin": origin, "deployment_id": snapshot.status["deployment_id"],
            "slots": snapshot.status["slots"], "bytes": sum(map(len, snapshot.files.values()))}


def verify_published(origin: str, expected_dir: Path | None = None, *, reader: Callable | None = None,
                     now: datetime | None = None, wait_seconds: int = 0) -> dict:
    """Read a complete weather/viewer snapshot with bounded post-upload retries."""
    origin = origin_url(origin)
    require(type(wait_seconds) is int and 0 <= wait_seconds <= 120,
            "Deployment wait must be an integer from 0 to 120 seconds")
    require(not wait_seconds or expected_dir is not None, "Deployment wait requires an expected directory")
    expected = None
    if expected_dir is not None:
        local = local_snapshot(expected_dir, now=now, include_frontend=True)
        expected = local.files["status.json"]
    started = monotonic()
    retry_deadline, deadline = started + wait_seconds, started + MAX_VERIFICATION_SECONDS
    source = reader or HTTPReader(origin, deadline=deadline)
    requests, reserved_bytes, attempts = 0, 0, 0

    def read(path, limit):
        nonlocal requests, reserved_bytes
        if monotonic() >= deadline:
            raise VerificationTimeout("Publication readback exceeded its 300-second budget")
        requests += 1
        # Reserve the maximum response before issuing a request, including failed
        # and partial reads. This bounds all attempts without trusting received data.
        reserved_bytes += limit + 1
        require(requests <= MAX_VERIFICATION_REQUESTS, "Publication exhausted its cumulative request budget")
        require(reserved_bytes <= MAX_VERIFICATION_BYTES, "Publication exhausted its cumulative byte budget")
        body = source(path, limit)
        if monotonic() >= deadline:
            raise VerificationTimeout("Publication readback exceeded its 300-second budget")
        return body

    while True:
        attempts += 1
        try:
            snapshot = snapshot_from_reader(read, now=now, expected_status=expected,
                                            frontend_paths=PUBLIC_FRONTEND, allow_analytics=True)
            break
        except (PublicationChanged, TransientReadError, MissingRemote) as exc:
            remaining = min(retry_deadline, deadline) - monotonic()
            if remaining <= 0:
                raise
            delay = min(5, remaining)
            # Keep stdout machine-readable for the workflow's publication proof.
            print(f"{exc}; waiting {delay:g}s for publication", file=sys.stderr, flush=True)
            sleep(delay)
            if monotonic() >= min(retry_deadline, deadline):
                raise
    if monotonic() >= deadline:
        raise VerificationTimeout("Publication readback exceeded its 300-second budget")
    verified_at = common.timestamp(now or common.utc_now()).astimezone(JST)
    return {"verified": True, "origin": origin, "deployment_id": snapshot.status["deployment_id"],
            "slots": snapshot.status["slots"], "files_verified": len(snapshot.files),
            "bytes_verified": sum(map(len, snapshot.files.values())),
            "attempts": attempts, "requests": requests,
            "frontend_verified": list(PUBLIC_FRONTEND),
            "root_html_transform": ("none" if common.sha256(snapshot.files["index.html"])
                                    == snapshot.status["frontend_sha256"]["index.html"]
                                    else "pinned_cloudflare_analytics"),
            "verified_publication_at_jst": verified_at.isoformat(),
            "deadline_met": {slot: verified_at <= common.timestamp(pointer["publication_deadline_jst"])
                             for slot, pointer in snapshot.pointers.items()},
            "deadline_assessment_basis": "Successful full readback is an upper bound on publication time; late verification alone does not prove late initial availability."}
