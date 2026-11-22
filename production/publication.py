"""Commit complete daily sheets to Blob, using immutable assets and one pointer.

The authoritative public object is latest/{slot}.json. PNG/PDF are immutable:
there is deliberately no separately mutable image alias that could get ahead
of the pointer. A slot lease spans generation and commit. Its seven-minute
lifetime exceeds the deployed function's hard 300-second execution limit.
Blob PUT has no destination If-Match; this is therefore a bounded-function
lease, not a general-purpose lock for unbounded workers.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import threading
from itertools import islice
from typing import Callable, Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
import uuid

UTC = timezone.utc
JST = timezone(timedelta(hours=9))
SLOTS = {"0900": 9, "1400": 14}
LEASE_SECONDS = 420
POINTER_SCHEMA = "kyushu-daily-pointer-1"
NUMERIC_FIELDS = ("temperature_c", "cas_kt", "tas_kt", "wind_from_true_deg",
                  "wind_speed_kt", "fuel_per_minute_us_gal")
ALTITUDES = [9500, 8500, 7500, 6500, 5500, 4500, 3500, 3000, 2500]


class PublicationError(Exception):
    code = "publication_failed"


class StorageError(PublicationError):
    code = "storage_failed"


class ObjectMissing(StorageError):
    code = "object_missing"


class ObjectConflict(StorageError):
    code = "object_conflict"


class LeaseBusy(PublicationError):
    code = "refresh_in_progress"


class LeaseLost(PublicationError):
    code = "lease_lost"


class InvalidPackage(PublicationError):
    code = "invalid_publication_package"


class StalePublication(PublicationError):
    code = "stale_publication_rejected"


def utc_now() -> datetime:
    return datetime.now(UTC)


def timestamp(value: str | datetime) -> datetime:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError) as exc:
        raise InvalidPackage("A valid timestamp is required") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise InvalidPackage("Timestamps must include a UTC offset")
    return result.astimezone(UTC)


def day_string(value: str) -> str:
    try:
        result = date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise InvalidPackage("A valid ISO date is required") from exc
    if value != result:
        raise InvalidPackage("Date must be YYYY-MM-DD")
    return result


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False,
                       sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def json_object(body: bytes) -> dict:
    def reject(value):
        raise InvalidPackage("JSON contains a nonfinite value")
    try:
        result = json.loads(body, parse_constant=reject)
    except (ValueError, UnicodeError) as exc:
        raise InvalidPackage("Invalid JSON object") from exc
    if not isinstance(result, dict):
        raise InvalidPackage("A JSON object is required")
    return result


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def safe_key(key: str) -> str:
    p = PurePosixPath(key)
    if not key or p.is_absolute() or ".." in p.parts or str(p) != key:
        raise InvalidPackage("Invalid object path")
    return key


@dataclass(frozen=True)
class StoredObject:
    path: str
    body: bytes
    etag: str
    url: str
    download_url: str
    content_type: str


class ObjectStore(Protocol):
    def read(self, key: str) -> StoredObject: ...
    def put(self, key: str, body: bytes, *, content_type: str,
            overwrite: bool = False, immutable: bool = False) -> StoredObject: ...
    def delete(self, key: str, *, if_match: str) -> None: ...
    def iter_keys(self, prefix: str, *, limit: int) -> list[str]: ...
    def delete_many(self, keys: list[str]) -> None: ...


class LocalObjectStore:
    """Filesystem fake used by failure/ordering tests; never used by production."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, key: str) -> Path:
        return self.directory / safe_key(key)

    def _record(self, key: str, body: bytes, content_type="application/octet-stream") -> StoredObject:
        url = "https://offline.invalid/" + key
        return StoredObject(key, body, sha256(body), url, url, content_type)

    def read(self, key: str) -> StoredObject:
        with self._lock:
            try:
                return self._record(key, self._path(key).read_bytes())
            except FileNotFoundError as exc:
                raise ObjectMissing(key) from exc

    def put(self, key: str, body: bytes, *, content_type: str,
            overwrite: bool = False, immutable: bool = False) -> StoredObject:
        with self._lock:
            path = self._path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not overwrite and path.exists():
                raise ObjectConflict(key)
            staging = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                staging.write_bytes(body)
                if overwrite:
                    os.replace(staging, path)
                else:
                    try:
                        os.link(staging, path)  # Atomic exclusive creation, even across instances.
                    except FileExistsError as exc:
                        raise ObjectConflict(key) from exc
            finally:
                staging.unlink(missing_ok=True)
            return self._record(key, body, content_type)

    def delete(self, key: str, *, if_match: str) -> None:
        with self._lock:
            record = self.read(key)
            if record.etag != if_match:
                raise ObjectConflict("Conditional delete rejected")
            self._path(key).unlink()

    def iter_keys(self, prefix: str, *, limit: int) -> list[str]:
        return sorted(str(p.relative_to(self.directory)) for p in self.directory.rglob("*")
                      if p.is_file() and str(p.relative_to(self.directory)).startswith(prefix))[:limit]

    def delete_many(self, keys: list[str]) -> None:
        with self._lock:
            for key in keys:
                self._path(key).unlink(missing_ok=True)


class BlobObjectStore:
    """Official vercel-blob >= 0.5 SDK; credentials remain inside SDK/OIDC."""

    def __init__(self):
        if not os.environ.get("BLOB_STORE_ID", "").strip():
            raise StorageError("Blob storage is not configured (BLOB_STORE_ID)")
        from vercel.blob import sync
        from vercel.blob.errors import BlobError, BlobNotFoundError, BlobPreconditionFailedError
        self.sdk = sync
        self.error = BlobError
        self.not_found = BlobNotFoundError
        self.precondition = BlobPreconditionFailedError

    @staticmethod
    def _etag(value: str) -> str:
        return value.removeprefix("W/").strip('"')

    def read(self, key: str) -> StoredObject:
        safe_key(key)
        try:
            # HEAD queries authoritative API metadata. GET is CDN-delivered;
            # cache bust and compare its ETag so stale mutable JSON fails closed.
            for _ in range(2):
                head = self.sdk.head_blob(key)
                split = urlsplit(head.url)
                query = parse_qsl(split.query) + [("_revision", uuid.uuid4().hex)]
                url = urlunsplit((split.scheme, split.netloc, split.path, urlencode(query), split.fragment))
                got = self.sdk.get_blob(url, access="public")
                if self._etag(got.metadata.etag) != self._etag(head.etag):
                    continue
                if len(got.body) != head.size:
                    raise StorageError("Blob read length did not match authoritative metadata")
                return StoredObject(key, got.body, head.etag, head.url, head.download_url,
                                    head.content_type or "application/octet-stream")
            raise ObjectConflict("Blob changed while being read")
        except self.not_found as exc:
            raise ObjectMissing(key) from exc
        except self.error as exc:
            raise StorageError("Blob read failed") from exc

    def put(self, key: str, body: bytes, *, content_type: str,
            overwrite: bool = False, immutable: bool = False) -> StoredObject:
        safe_key(key)
        try:
            result = self.sdk.put_blob(key, body, access="public", content_type=content_type,
                                       add_random_suffix=False, allow_overwrite=overwrite,
                                       cache_control_max_age=31536000 if immutable else 60)
            return StoredObject(key, body, result.etag, result.url, result.download_url, content_type)
        except self.error as exc:
            # SDK has no AlreadyExists class. Never turn an auth/service error
            # into a lock conflict. A non-overwrite failure is a conflict only
            # when the existing object can independently be read successfully.
            if not overwrite and getattr(exc, "status_code", None) not in (401, 403, 429, 503):
                try:
                    self.read(key)
                except ObjectMissing:
                    pass
                else:
                    raise ObjectConflict(key) from exc
            raise StorageError("Blob upload failed") from exc

    def delete(self, key: str, *, if_match: str) -> None:
        try:
            self.sdk.delete_blob(safe_key(key), if_match=if_match)
        except self.not_found as exc:
            raise ObjectMissing(key) from exc
        except self.precondition as exc:
            raise ObjectConflict("Conditional Blob deletion rejected") from exc
        except self.error as exc:
            raise StorageError("Blob deletion failed") from exc

    def iter_keys(self, prefix: str, *, limit: int) -> list[str]:
        try:
            return [item.pathname for item in islice(
                self.sdk.query_blobs(prefix=prefix, page_size=min(limit, 1000)), limit)]
        except self.error as exc:
            raise StorageError("Blob listing failed") from exc

    def delete_many(self, keys: list[str]) -> None:
        if not keys or len(keys) > 1000:
            raise InvalidPackage("Cleanup requires between 1 and 1000 verified paths")
        try:
            self.sdk.delete_blob([safe_key(key) for key in keys])
        except self.error as exc:
            raise StorageError("Old release cleanup failed") from exc


@contextmanager
def blob_request_context(headers: dict[str, str]):
    """Scope OIDC headers and bounded SDK socket timeouts to this invocation."""
    import httpx2 as httpx
    from vercel.api import session
    from vercel.headers import HeadersContext
    with HeadersContext(headers).use(), session(
            httpx_client_factory=lambda: httpx.Client(timeout=httpx.Timeout(12.0, connect=5.0))):
        yield


class SlotLease:
    def __init__(self, store: ObjectStore, slot: str, *, clock: Callable = utc_now,
                 owner: str | None = None):
        if slot not in SLOTS:
            raise InvalidPackage("Unknown forecast slot")
        self.store, self.slot, self.clock = store, slot, clock
        self.owner = owner or uuid.uuid4().hex
        self.key = "leases/" + slot + ".json"
        self.record: StoredObject | None = None
        self.expires_at: datetime | None = None

    def __enter__(self):
        now = timestamp(self.clock())
        self.expires_at = now + timedelta(seconds=LEASE_SECONDS)
        body = json_bytes({"schema": "kyushu-slot-lease-1", "slot": self.slot,
                           "owner": self.owner, "acquired_at": now.isoformat(),
                           "expires_at": self.expires_at.isoformat()})
        for _ in range(2):
            try:
                self.record = self.store.put(self.key, body, content_type="application/json")
                self.assert_owned()
                return self
            except ObjectConflict:
                try:
                    old = self.store.read(self.key)
                except ObjectMissing:
                    continue
                value = json_object(old.body)
                if (value.get("schema") != "kyushu-slot-lease-1" or
                        value.get("slot") != self.slot or not value.get("owner")):
                    raise LeaseBusy("Unrecognized existing lease; refusing to remove it")
                if timestamp(value["expires_at"]) > now:
                    raise LeaseBusy("Another invocation owns this slot")
                try:
                    self.store.delete(self.key, if_match=old.etag)
                except (ObjectMissing, ObjectConflict):
                    continue
        raise LeaseBusy("Another invocation acquired this slot")

    def assert_owned(self):
        if self.record is None or self.expires_at is None:
            raise LeaseLost("No publication lease")
        if timestamp(self.clock()) + timedelta(seconds=20) >= self.expires_at:
            raise LeaseLost("Publication lease is too close to expiry")
        try:
            got = self.store.read(self.key)
        except ObjectMissing as exc:
            raise LeaseLost("Publication lease disappeared") from exc
        value = json_object(got.body)
        if got.etag != self.record.etag or value.get("owner") != self.owner:
            raise LeaseLost("Another owner replaced the publication lease")

    def __exit__(self, exc_type, exc, traceback):
        if self.record is not None:
            try:
                self.store.delete(self.key, if_match=self.record.etag)
            except (ObjectMissing, ObjectConflict, StorageError):
                # A completed pointer is authoritative even if cleanup fails;
                # an abandoned lease can be reclaimed after its bounded expiry.
                pass


def read_latest(store: ObjectStore, slot: str) -> dict | None:
    if slot not in SLOTS:
        raise InvalidPackage("Unknown forecast slot")
    try:
        value = json_object(store.read("latest/" + slot + ".json").body)
    except ObjectMissing:
        return None
    if value.get("schema") != POINTER_SCHEMA or value.get("slot") != slot:
        raise InvalidPackage("Existing public pointer has an unexpected schema")
    day_string(value.get("valid_date"))
    for key in ("initial_time_jst", "image_generated_time_jst", "published_at_jst"):
        timestamp(value.get(key))
    if value.get("model") not in ("MSM", "GSM") or value.get("quality") not in ("complete", "partial"):
        raise InvalidPackage("Existing public pointer has invalid source/quality")
    for kind in ("png", "pdf", "weather_json"):
        if not value.get("files", {}).get(kind, {}).get("url"):
            raise InvalidPackage("Existing public pointer lacks a completed asset")
    return value


def complete_msm_exists(current: dict | None, target_date: str) -> bool:
    return bool(current and current["valid_date"] == target_date and
                current["model"] == "MSM" and current["quality"] == "complete")


def replacement_allowed(current: dict | None, candidate: dict) -> None:
    if current is None:
        return
    if candidate["valid_date"] < current["valid_date"]:
        raise StalePublication("An older forecast date cannot replace the public sheet")
    if candidate["valid_date"] > current["valid_date"]:
        return
    if current["quality"] == "complete" and candidate["quality"] != "complete":
        raise StalePublication("Incomplete coverage cannot replace a complete same-day sheet")
    if current["model"] == "MSM" and candidate["model"] != "MSM":
        raise StalePublication("GSM cannot replace a successful same-day MSM sheet")
    if current["model"] != candidate["model"]:
        return  # A coherent preferred MSM run may replace a newer GSM fallback.
    if timestamp(candidate["initial_time_jst"]) < timestamp(current["initial_time_jst"]):
        raise StalePublication("An older source run cannot replace a newer one")
    if timestamp(candidate["image_generated_time_jst"]) <= timestamp(current["image_generated_time_jst"]):
        raise StalePublication("An older or duplicate generation cannot replace the public sheet")


def _local_asset(directory: Path, record: dict, media_type: str) -> bytes:
    path = record.get("path", "")
    if not path or Path(path).name != path or record.get("media_type") != media_type:
        raise InvalidPackage("Unexpected artifact path or content type")
    resolved = (directory / path).resolve()
    if resolved.parent != directory.resolve():
        raise InvalidPackage("Artifact escaped its output directory")
    try:
        body = resolved.read_bytes()
    except OSError as exc:
        raise InvalidPackage("A required artifact is absent") from exc
    if (not body or record.get("bytes") != len(body) or
            record.get("sha256") != sha256(body)):
        raise InvalidPackage("Artifact size or digest mismatch")
    return body


def validate_package(manifest: dict, directory: Path, target_date: str, slot: str,
                     *, now: datetime) -> tuple[dict, dict[str, tuple[dict, bytes]]]:
    """Validate actual rows, terrain reasons, timestamps and all local digests."""
    day_string(target_date)
    if slot not in SLOTS or manifest.get("schema") != "kyushu-daily-publication-1":
        raise InvalidPackage("Unknown publication contract")
    if (manifest.get("target_date") != target_date or
            manifest.get("timezone") != "Asia/Tokyo" or len(manifest.get("forecasts", [])) != 1):
        raise InvalidPackage("Publish exactly one slot for the requested JST date")
    entry = dict(manifest["forecasts"][0])
    expected = datetime.combine(date.fromisoformat(target_date), time(SLOTS[slot]), JST)
    if (entry.get("slot") != slot or entry.get("valid_date") != target_date or
            timestamp(entry.get("valid_at_jst")) != expected or
            entry.get("valid_time_jst") != f"{SLOTS[slot]:02d}:00"):
        raise InvalidPackage("Forecast valid date/time does not match the requested slot")
    now = timestamp(now)
    generated = timestamp(entry.get("image_generated_time_jst"))
    initial = timestamp(entry.get("initial_time_jst"))
    if (entry.get("historical") or not entry.get("target_date_matches_generation_date") or
            now.astimezone(JST).date().isoformat() != target_date or
            generated.astimezone(JST).date().isoformat() != target_date):
        raise InvalidPackage("A historical sheet cannot become today's public output")
    if generated > now + timedelta(seconds=5) or initial > generated:
        raise InvalidPackage("Publication timestamps are in the future")
    if timestamp(entry.get("initial_time_utc")) != initial or entry.get("model") not in ("MSM", "GSM"):
        raise InvalidPackage("Missing or inconsistent source run")
    assets = {}
    for kind, media_type in (("png", "image/png"), ("pdf", "application/pdf")):
        record = entry.get("files", {}).get(kind, {})
        assets[kind] = (record, _local_asset(directory, record, media_type))
    record = manifest.get("forecast_json", {})
    assets["weather_json"] = (record, _local_asset(directory, record, "application/json"))
    weather = json_object(assets["weather_json"][1])
    if weather.get("schema") != "pseudo-kyushu-paper-forecast-1" or len(weather.get("forecasts", [])) != 1:
        raise InvalidPackage("Weather JSON must contain exactly one forecast")
    forecast = weather["forecasts"][0]
    metadata = forecast.get("metadata", {})
    if (metadata.get("target_date") != target_date or metadata.get("valid_time_jst") != entry["valid_time_jst"] or
            metadata.get("model") != entry["model"] or timestamp(metadata.get("initial_time_utc")) != initial or
            metadata.get("source_acquisition_failures", 0) or metadata.get("source_missing_forecasts", 0)):
        raise InvalidPackage("Weather source/date does not match the rendered publication")
    cutoff = timestamp(metadata.get("as_of_cutoff_utc"))
    deadline = datetime.combine(date.fromisoformat(target_date), time(5 if slot == "0900" else 10), JST)
    if initial > cutoff or cutoff > min(now, deadline):
        raise InvalidPackage("Weather source was not available by its acquisition cutoff")
    groups = forecast.get("groups", [])
    if len(groups) != 10 or sorted(g.get("id", "") for g in groups) != list("ABCDEFGHIJ"):
        raise InvalidPackage("All ten accepted sectors are required")
    rows = []
    for group in groups:
        if [r.get("altitude_ft_msl") for r in group.get("rows", [])] != ALTITUDES:
            raise InvalidPackage("Every sector must contain all nine descending altitudes")
        rows.extend(group["rows"])
    counts = {"valid": 0, "terrain": 0, "partial": 0}
    for row in rows:
        status = row.get("status")
        if status not in ("valid", "terrain"):
            raise InvalidPackage("A missing weather row cannot be published as successful")
        if row.get("source_acquisition_failures", 0) or row.get("source_nonfinite_count", 0):
            raise InvalidPackage("A row contains unresolved source acquisition/data loss")
        if row.get("mask_reason_counts", {}).get("outside_vertical_range_count", 0):
            raise InvalidPackage("A row extends beyond the source vertical range")
        if status == "valid":
            if any(isinstance(row.get(key), bool) or not isinstance(row.get(key), (int, float)) or
                   not math.isfinite(row[key]) for key in NUMERIC_FIELDS):
                raise InvalidPackage("A valid row lacks complete weather/performance numbers")
            if not row.get("fuel_per_minute_us_gal_display"):
                raise InvalidPackage("A valid row lacks the required fuel display")
        counts[status] += 1
        counts["partial"] += bool(row.get("partial_flag"))
    coverage = entry.get("coverage", {})
    if (not counts["valid"] or coverage.get("total_rows") != 90 or coverage.get("missing_rows") != 0 or
            coverage.get("terrain_rows") != counts["terrain"] or coverage.get("partial_rows") != counts["partial"] or
            coverage.get("complete_numeric_rows") != counts["valid"] or
            not coverage.get("all_nonterrain_rows_complete")):
        raise InvalidPackage("Rendered coverage does not match the actual weather rows")
    entry["quality"] = "partial" if counts["partial"] else "complete"
    entry["publication_deadline_jst"] = deadline.isoformat()
    return entry, assets


def publish_package(store: ObjectStore, manifest: dict, directory: Path, *,
                    target_date: str, slot: str, lease: SlotLease,
                    clock: Callable = utc_now, check_budget: Callable = lambda: None) -> dict:
    entry, assets = validate_package(manifest, directory, target_date, slot, now=clock())
    lease.assert_owned()
    current = read_latest(store, slot)
    replacement_allowed(current, entry)
    release_id = timestamp(clock()).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:12]
    prefix = f"releases/{target_date}/{slot}/{release_id}/"
    public_files = {}
    for kind, (local, body) in assets.items():
        check_budget()
        key = prefix + local["path"]
        stored = store.put(key, body, content_type=local["media_type"], immutable=True)
        checked = store.read(key)
        if (checked.etag != stored.etag or len(checked.body) != local["bytes"] or
                sha256(checked.body) != local["sha256"]):
            raise StorageError("Uploaded artifact failed readback verification")
        public_files[kind] = {**local, "path": key, "url": stored.url,
                              "download_url": stored.download_url}
    check_budget()
    public_manifest = {**manifest, "release_id": release_id, "published_files": public_files}
    manifest_bytes = json_bytes(public_manifest)
    stored_manifest = store.put(prefix + "manifest.json", manifest_bytes,
                                content_type="application/json", immutable=True)
    if store.read(stored_manifest.path).body != manifest_bytes:
        raise StorageError("Release manifest failed readback verification")
    check_budget()
    # Recheck lease and pointer immediately before the only public commit.
    # All preceding failures leave the previous pointer and both its images intact.
    lease.assert_owned()
    replacement_allowed(read_latest(store, slot), entry)
    published_at = timestamp(clock()).astimezone(JST)
    pointer = {**entry, "schema": POINTER_SCHEMA, "release_id": release_id,
               "files": public_files, "manifest_url": stored_manifest.url,
               "published_at_jst": published_at.isoformat(),
               "deadline_met": published_at <= timestamp(entry["publication_deadline_jst"])}
    pointer_body = json_bytes(pointer)
    pointer_key = f"latest/{slot}.json"
    try:
        committed = store.put(pointer_key, pointer_body, content_type="application/json", overwrite=True)
    except StorageError:
        # A timeout may follow a successful write. Resolve that ambiguity by
        # reading back; never blindly retry a mutable write over a newer release.
        if store.read(pointer_key).body != pointer_body:
            raise
    else:
        checked = store.read(pointer_key)
        if checked.body != pointer_body or checked.etag != committed.etag:
            raise StorageError("Public commit could not be verified")
    return pointer


def cleanup_old_releases(store: ObjectStore, *, now: datetime, retention_days: int = 31,
                         limit: int = 1000, check_budget: Callable = lambda: None) -> dict:
    """Bounded cleanup after a successful commit; never delete either latest.

    Only our strict release path and known artifact names are eligible. Completed
    releases additionally require their publication schema/identity to match.
    A missing manifest identifies abandoned staging only after the 31-day wait;
    its strict generated path is eligible too. Foreign/malformed objects remain.
    """
    if retention_days < 31 or not 1 <= limit <= 1000:
        raise InvalidPackage("Retention must be at least 31 days; cleanup limit is 1000")
    cutoff = timestamp(now).astimezone(JST).date() - timedelta(days=retention_days)
    protected = set()
    for slot in SLOTS:
        check_budget()
        current = read_latest(store, slot)
        if current:
            protected.update(str(PurePosixPath(asset["path"]).parent) + "/"
                             for asset in current["files"].values())
    check_budget()
    keys = store.iter_keys("releases/", limit=limit)
    groups = {}
    pattern = re.compile(
        r"^(releases/(\d{4}-\d{2}-\d{2})/(0900|1400)/(\d{8}T\d{12}Z-[0-9a-f]{12})/)([^/]+)$")
    for key in keys:
        match = pattern.fullmatch(key)
        if not match:
            continue
        prefix, day, slot, release_id, basename = match.groups()
        try:
            release_day = date.fromisoformat(day)
            generated = datetime.strptime(release_id.split("-")[0], "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
        except ValueError:
            continue
        if prefix in protected or release_day >= cutoff or generated.astimezone(JST).date() != release_day:
            continue
        date_tag = day.replace("-", "")
        known = {"manifest.json", f"kyushu_pseudo_{slot}_{date_tag}.png",
                 f"kyushu_pseudo_{slot}_{date_tag}.pdf", f"kyushu_pseudo_forecasts_{date_tag}_{slot}.json"}
        if basename in known:
            group = groups.setdefault(prefix, {"keys": [], "day": day, "slot": slot, "release_id": release_id})
            group["keys"].append(key)
    eligible = []
    for prefix, group in groups.items():
        check_budget()
        try:
            manifest = json_object(store.read(prefix + "manifest.json").body)
        except ObjectMissing:
            pass  # Old abandoned staging; no current pointer references it.
        else:
            entries = manifest.get("forecasts", [])
            if (manifest.get("schema") != "kyushu-daily-publication-1" or
                    manifest.get("release_id") != group["release_id"] or
                    manifest.get("target_date") != group["day"] or len(entries) != 1 or
                    entries[0].get("slot") != group["slot"]):
                continue
        eligible.extend(group["keys"])
    if eligible:
        check_budget()
        store.delete_many(eligible[:limit])
    return {"status": "completed", "retention_days": retention_days,
            "scanned_objects": len(keys), "deleted_objects": min(len(eligible), limit)}
