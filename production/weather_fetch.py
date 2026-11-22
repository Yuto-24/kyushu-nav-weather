"""Acquire one coherent JMA pressure-level forecast from RISH.

The download/GRIB validation follows analysis/fetch_msm.py. This module changes
run selection only: it discovers available runs at a supplied cutoff and can
fall back to GSM. It never relabels a cached forecast's valid time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
from pathlib import Path
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np

UTC = timezone.utc
BASE = "https://database.rish.kyoto-u.ac.jp/arch/jmadata/data/gpv/original"
LEVELS = (1000, 975, 950, 925, 900, 850, 800, 700, 600)
FIELDS = {(2, 2): ("u", "u"), (2, 3): ("v", "v"),
          (0, 0): ("t", "tmp"), (3, 5): ("gh", "hgt")}
USER_AGENT = "Kyushu-navlog-weather/1.0"


def digest(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    allow_nan=False) + "\n")
    temporary.replace(path)


def aware(value):
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("An explicit UTC offset is required.")
    return value.astimezone(UTC)


def bracket_hours(run, target):
    hours = (target - run).total_seconds() / 3600
    lower = math.floor(hours / 3) * 3
    upper = math.ceil(hours / 3) * 3
    return (lower,) if lower == upper else (lower, upper)


@dataclass(frozen=True)
class Candidate:
    model: str
    run: datetime
    hours: tuple[int, ...]
    urls: tuple[str, ...]

    @property
    def valid_times(self):
        return tuple(self.run + timedelta(hours=h) for h in self.hours)


def _fd(hour):
    return f"{hour // 24:02d}{hour % 24:02d}"


def candidate_for(model, run, target):
    hours = bracket_hours(run, target)
    if min(hours) < 0:
        return None
    if model == "MSM":
        # The 39-hour range is sufficient for this daily same-day product.
        intervals = ((0, 15), (18, 33), (36, 39))
        pattern = "Z__C_RJTD_{run}_MSM_GPV_Rjp_L-pall_FH{a:02d}-{b:02d}_grib2.bin"
    elif model == "GSM":
        # Corrected JMA TI 601: FD means days/hours, not forecast hours.
        intervals = ((0, 24), (27, 48), (51, 72))
        pattern = "Z__C_RJTD_{run}_GSM_GPV_Rjp_Gll0p1deg_L-pall_FD{fa}-{fb}_grib2.bin"
    else:
        raise ValueError(model)
    names = []
    for hour in hours:
        interval = next(((a, b) for a, b in intervals if a <= hour <= b), None)
        if interval is None:
            return None
        a, b = interval
        name = pattern.format(run=run.strftime("%Y%m%d%H%M%S"), a=a, b=b,
                              fa=_fd(a), fb=_fd(b))
        if name not in names:
            names.append(name)
    return Candidate(model, run, hours,
                     tuple(f"{BASE}/{run:%Y/%m/%d}/{name}" for name in names))


def candidates(target, as_of, models=("MSM", "GSM")):
    """MSM priority, then newest-to-oldest within each model.

    No estimated publication lag is used to call a run available. HEAD and the
    actual GET must both establish availability no later than the cutoff.
    """
    for model in models:
        step = 3 if model == "MSM" else 6
        latest = as_of.replace(hour=(as_of.hour // step) * step,
                               minute=0, second=0, microsecond=0)
        max_age = 36 if model == "MSM" else 48
        for offset in range(0, max_age + 1, step):
            run = latest - timedelta(hours=offset)
            item = candidate_for(model, run, target)
            if item is not None and run <= as_of:
                yield item


def _availability(headers, as_of):
    value = headers.get("last-modified")
    if not value:
        raise ValueError("Source has no Last-Modified; cutoff eligibility is unverified")
    modified = parsedate_to_datetime(value).astimezone(UTC)
    if modified > as_of:
        raise ValueError(f"Source published after cutoff: {modified.isoformat()}")
    return modified


def _budget(deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError("Weather acquisition time budget exhausted")


def _timeout(deadline, maximum):
    _budget(deadline)
    return max(.1, min(maximum, deadline - time.monotonic()))


def _download(url, output, as_of, deadline):
    _budget(deadline)
    headers = {"User-Agent": USER_AGENT}
    with urlopen(Request(url, headers=headers, method="HEAD"), timeout=_timeout(deadline, 12)) as response:
        head = {k.lower(): v for k, v in response.headers.items()}
        _availability(head, as_of)
    _budget(deadline)
    hasher = hashlib.sha256()
    received = 0
    started = time.monotonic()
    with urlopen(Request(url, headers=headers), timeout=_timeout(deadline, 20)) as response, output.open("wb") as stream:
        observed = {k.lower(): v for k, v in response.headers.items()}
        modified = _availability(observed, as_of)
        final_url = response.url
        # read1 performs at most one underlying socket read. read(n) could wait
        # indefinitely for n bytes under a slow trickle despite socket timeout,
        # preventing the total elapsed-time check from regaining control.
        while True:
            _budget(deadline)
            chunk = response.read1(1024 * 1024)
            if not chunk:
                break
            _budget(deadline)
            stream.write(chunk)
            hasher.update(chunk)
            received += len(chunk)
    if observed.get("content-length") and received != int(observed["content-length"]):
        raise ValueError("Downloaded bytes disagree with Content-Length")
    if received < 8:
        raise ValueError("Empty or truncated GRIB response")
    with output.open("rb") as stream:
        first = stream.read(4)
        stream.seek(-4, 2)
        last = stream.read(4)
    if first != b"GRIB" or last != b"7777":
        raise ValueError("Incomplete or non-GRIB source response")
    return {"url": url, "response_url": final_url, "http_status": 200,
            "last_modified": observed["last-modified"],
            "last_modified_utc": modified.isoformat(), "headers": observed,
            "bytes": received, "sha256": hasher.hexdigest(),
            "downloaded_at_utc": datetime.now(UTC).isoformat(),
            "download_seconds": time.monotonic() - started}


def _decode(paths, candidate):
    import pygrib
    from jma_gpv_weather import Bounds
    from jma_gpv_weather.grib import read_grib_records

    # Padding is needed only for bilinear projection of GSM onto fixed MSM
    # sampling nodes. MSM values at those nodes are copied exactly.
    bounds = Bounds(30.2, 34.2, 129.1, 132.7)
    identities, messages = {}, set()
    total = 0
    for path in paths:
        grib = pygrib.open(str(path))
        try:
            total += grib.messages
            for message in grib:
                if message.typeOfLevel != "isobaricInhPa" or int(message.level) not in LEVELS:
                    continue
                pair = (int(message.parameterCategory), int(message.parameterNumber))
                if pair not in FIELDS:
                    continue
                valid = message.validDate.replace(tzinfo=UTC)
                if valid not in candidate.valid_times:
                    continue
                if message.analDate.replace(tzinfo=UTC) != candidate.run:
                    raise ValueError("GRIB initial time differs from candidate run")
                variable, _ = FIELDS[pair]
                identity = (valid, int(message.level), variable)
                if identity in messages:
                    raise ValueError("Duplicate required GRIB record")
                messages.add(identity)
                identities[variable] = {"shortName": message.shortName,
                    "name": message.name, "units": message.units}
        finally:
            grib.close()
    expected = {(valid, level, field) for valid in candidate.valid_times
                for level in LEVELS for field in ("u", "v", "t", "gh")}
    if messages != expected:
        raise ValueError(f"Required GRIB fields missing: {len(expected - messages)}")
    _, pressure = read_grib_records(tuple(paths), None, bounds, candidate.valid_times,
                                    pressure_levels=LEVELS)
    records = {}
    latitudes = longitudes = None
    for variable, library_variable in FIELDS.values():
        arrays = []
        for valid in candidate.valid_times:
            layers = []
            for level in LEVELS:
                values, lat, lon = pressure[valid, level, library_variable]
                if latitudes is None:
                    latitudes, longitudes = lat[:, 0].copy(), lon[0, :].copy()
                if not (np.array_equal(lat[:, 0], latitudes) and
                        np.array_equal(lon[0, :], longitudes)):
                    raise ValueError("Inconsistent GRIB grid across required fields")
                layers.append(values)
            arrays.append(np.stack(layers))
        records[variable] = np.stack(arrays)
    if any(not np.isfinite(v).all() for v in records.values()):
        raise ValueError("Nonfinite source values; trying another coherent forecast")
    if not (np.diff(records["gh"], axis=1) > 0).all():
        raise ValueError("Non-monotone pressure heights")
    return {**records, "lat": latitudes, "lon": longitudes,
            "levels_hpa": np.asarray(LEVELS), "forecast_hours": np.asarray(candidate.hours),
            "valid_times_utc": np.asarray([v.isoformat() for v in candidate.valid_times]),
            "run_initial_time_utc": np.asarray(candidate.run.isoformat())}, {
        "decoder": "jma_gpv_weather.grib.read_grib_records", "pygrib_version": pygrib.__version__,
        "total_grib_messages": total, "selected_message_count": len(messages),
        "field_identities": identities, "shape": list(records["u"].shape),
        "levels_hpa": list(LEVELS),
        "missing_values": {k: int((~np.isfinite(v)).sum()) for k, v in records.items()}}


def acquire_candidate(candidate, target, as_of, cache, deadline):
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    stem = f"{candidate.model.lower()}_{candidate.run:%Y%m%d%H%M%S}_{target:%Y%m%dT%H%MZ}"
    subset = cache / (stem + ".npz")
    manifest_path = cache / (stem + ".manifest.json")
    if subset.exists() and manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if (manifest.get("status") == "EXTRACTED_VERIFIED" and
                manifest["subset"]["sha256"] == digest(subset) and
                aware(manifest["run_initial_time_utc"]) == candidate.run and
                aware(manifest["target_valid_time_utc"]) == target and
                manifest["model"] == candidate.model):
            for source in manifest["sources"]:
                _availability(source["headers"], as_of)
            with np.load(subset, allow_pickle=False) as data:
                arrays = {k: data[k].copy() for k in data if k != "metadata_json"}
            return arrays, {**manifest, "reused_verified_cache": True}
        raise ValueError(f"Cache integrity failure: {subset.name}")
    with tempfile.TemporaryDirectory(prefix="raw_", dir=cache) as directory:
        paths, sources = [], []
        for i, url in enumerate(candidate.urls):
            path = Path(directory) / f"source_{i}.grib2"
            sources.append(_download(url, path, as_of, deadline))
            paths.append(path)
        _budget(deadline)
        arrays, extraction = _decode(paths, candidate)
        _budget(deadline)
    manifest = {"schema": "kyushu-daily-pressure-subset-1", "status": "EXTRACTED_VERIFIED",
        "model": candidate.model, "run_initial_time_utc": candidate.run.isoformat(),
        "target_valid_time_utc": target.isoformat(), "forecast_hours": list(candidate.hours),
        "source_valid_times_utc": [t.isoformat() for t in candidate.valid_times],
        "availability_checked_at_utc": datetime.now(UTC).isoformat(),
        "availability_cutoff_utc": as_of.isoformat(),
        "availability_basis": "Observed HTTP availability and Last-Modified not later than cutoff",
        "sources": sources, "extraction": extraction,
        "raw_disposition": "task-owned raw files removed after decode; original hashes retained"}
    temporary = subset.with_suffix(".npz.tmp")
    with temporary.open("wb") as stream:
        np.savez_compressed(stream, **arrays)
    with np.load(temporary, allow_pickle=False) as check:
        if not all(np.array_equal(check[k], value, equal_nan=True)
                   if value.dtype.kind in "fc" else np.array_equal(check[k], value)
                   for k, value in arrays.items()):
            raise ValueError("Subset readback differs from decoded source")
    temporary.replace(subset)
    manifest["subset"] = {"file": subset.name, "sha256": digest(subset),
                          "bytes": subset.stat().st_size}
    write_json(manifest_path, manifest)
    return arrays, manifest


class WeatherUnavailable(RuntimeError):
    def __init__(self, attempts):
        self.attempts = attempts
        super().__init__("No available coherent MSM/GSM forecast within cutoff and acquisition budget")


def acquire(target, as_of, cache, budget_seconds=230, models=("MSM", "GSM")):
    if target.tzinfo is None or as_of.tzinfo is None:
        raise ValueError("Target and cutoff must include timezone")
    target, as_of = target.astimezone(UTC), as_of.astimezone(UTC)
    if as_of > datetime.now(UTC) + timedelta(seconds=1):
        raise ValueError("Availability cutoff must not be in the future")
    attempts = []
    if budget_seconds <= 0:
        raise ValueError("Acquisition budget must be positive")
    started = time.monotonic()
    deadline = started + budget_seconds
    # Reserve time for a real GSM attempt if the MSM source is unresponsive.
    msm_deadline = started + budget_seconds * .65 if {"MSM", "GSM"} <= set(models) else deadline
    for candidate in candidates(target, as_of, models):
        if time.monotonic() >= deadline:
            break
        candidate_deadline = msm_deadline if candidate.model == "MSM" else deadline
        if time.monotonic() >= candidate_deadline:
            continue
        attempt = {"model": candidate.model, "run_initial_time_utc": candidate.run.isoformat(),
                   "forecast_hours": list(candidate.hours), "urls": list(candidate.urls)}
        try:
            arrays, manifest = acquire_candidate(candidate, target, as_of, cache, candidate_deadline)
            attempts.append({**attempt, "status": "SELECTED"})
            manifest = {**manifest, "candidate_attempts": attempts,
                        "fallback_used": candidate.model != "MSM" or len(attempts) > 1,
                        "fallback_model_used": candidate.model != "MSM",
                        "acquisition_budget_seconds": budget_seconds,
                        "acquisition_elapsed_seconds": time.monotonic() - started,
                        "msm_phase_budget_seconds": msm_deadline - started,
                        "msm_phase_budget_exhausted": "MSM" in models and candidate.model != "MSM" and time.monotonic() >= msm_deadline}
            return arrays, manifest
        except (OSError, ValueError, KeyError, RuntimeError, HTTPError, URLError) as error:
            attempts.append({**attempt, "status": "REJECTED", "error_type": type(error).__name__,
                             "error": str(error), "http_status": getattr(error, "code", None)})
            print(json.dumps({"stage": "candidate_rejected", **attempts[-1]}, ensure_ascii=False),
                  flush=True)
    raise WeatherUnavailable(attempts)
