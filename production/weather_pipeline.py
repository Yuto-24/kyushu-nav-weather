#!/usr/bin/env python3
"""Build current Kyushu pseudo-sector forecasts without historical case arrays.

Example: python production/weather_pipeline.py --date 2026-11-22 --hours 9
         --output output/2026-11-22/0900.json --cache .weather-cache
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import math
from pathlib import Path
import sys

import numpy as np

if __package__:
    from .weather_fetch import UTC, WeatherUnavailable, acquire, aware, digest, write_json
else:
    from weather_fetch import UTC, WeatherUnavailable, acquire, aware, digest, write_json

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "weather_assets"
JST = timezone(timedelta(hours=9))
ALTITUDES = [9500, 8500, 7500, 6500, 5500, 4500, 3500, 3000, 2500]
MASK_NAMES = ["target_at_or_below_terrain", "lower_bracket_below_terrain",
              "outside_vertical_range", "source_temperature_nonfinite",
              "source_wind_nonfinite", "source_height_nonfinite"]


def read(path):
    return json.loads(Path(path).read_text())


def load_assets(directory=ASSETS):
    directory = Path(directory)
    manifest = read(directory / "asset_manifest.json")
    for relative, expected in manifest["files"].items():
        if digest(directory / relative) != expected:
            raise ValueError(f"Fixed asset hash mismatch: {relative}")
    with np.load(directory / "fixed_leg_weights.npz", allow_pickle=False) as data:
        arrays = {k: data[k].copy() for k in data}
    if not np.allclose(arrays["native_distance_weights_km"].sum(axis=1),
                       arrays["leg_lengths_km"], rtol=0, atol=1e-9):
        raise ValueError("Fixed distance weights do not sum to leg lengths")
    selection = read(directory / "selection_10.json")
    inventory = {r["id"]: r for r in read(directory / "leg_inventory.json")["legs"]}
    groups = [{"id": r["sector_id"], "representative_leg_id": r["leg_id"],
               "midpoint_lon_lat": r["midpoint_lon_lat"],
               "assigned_ids": sorted(r["assigned_known_leg_ids"])}
              for r in selection["representatives"]]
    expected = dict(zip("ABCDEFGHIJ", ("L16", "L25", "L31", "L21", "L09", "L05", "L04", "L03", "L27", "L07")))
    if {g["id"]: g["representative_leg_id"] for g in groups} != expected:
        raise ValueError("The adopted representative set changed")
    snapshot = directory / "navmate_snapshot"
    sys.path.insert(0, str(snapshot))
    from autonavlog.performance.cruise import CruisePerformanceSelectionPolicy
    from autonavlog.performance.repository import PerformanceRepository
    repository = PerformanceRepository.from_directory(snapshot / "data/performance")
    policy = CruisePerformanceSelectionPolicy(repository.cruise_rows, use_table_boundaries=True)
    return {**arrays, "directory": directory, "groups": groups, "inventory": inventory,
            "selection": selection, "performance_source": read(snapshot / "source_manifest.json"),
            "performance_fingerprint": repository.content_fingerprint, "policy": policy}


def project_to_fixed_nodes(data, xy, model):
    """Copy exact MSM nodes; only GSM fallback may require bilinear projection."""
    lat, lon = data["lat"], data["lon"]
    yi = np.abs(lat[:, None] - xy[None, :, 1]).argmin(axis=0)
    xi = np.abs(lon[:, None] - xy[None, :, 0]).argmin(axis=0)
    exact = np.allclose(lat[yi], xy[:, 1], rtol=0, atol=1e-8) and np.allclose(lon[xi], xy[:, 0], rtol=0, atol=1e-8)
    if exact:
        return {k: data[k][..., yi, xi] for k in ("u", "v", "t", "gh")}, "exact_native_coordinates"
    if model == "MSM":
        raise ValueError("MSM native pressure grid differs from the adopted fixed sampling grid")
    lat_order, lon_order = np.argsort(lat), np.argsort(lon)
    lat, lon = lat[lat_order], lon[lon_order]
    if ((xy[:, 1] < lat[0]).any() or (xy[:, 1] > lat[-1]).any() or
            (xy[:, 0] < lon[0]).any() or (xy[:, 0] > lon[-1]).any()):
        raise ValueError("Fallback source does not cover every fixed sample node")
    iy = np.clip(np.searchsorted(lat, xy[:, 1], side="right") - 1, 0, len(lat) - 2)
    ix = np.clip(np.searchsorted(lon, xy[:, 0], side="right") - 1, 0, len(lon) - 2)
    fy = (xy[:, 1] - lat[iy]) / (lat[iy + 1] - lat[iy])
    fx = (xy[:, 0] - lon[ix]) / (lon[ix + 1] - lon[ix])
    result = {}
    for key in ("u", "v", "t", "gh"):
        f = data[key][:, :, lat_order, :][:, :, :, lon_order]
        result[key] = ((1 - fy) * ((1 - fx) * f[..., iy, ix] + fx * f[..., iy, ix + 1]) +
                       fy * ((1 - fx) * f[..., iy + 1, ix] + fx * f[..., iy + 1, ix + 1]))
    return result, "GSM_pressure_profiles_bilinear_to_fixed_MSM_sample_coordinates"


def interpolate(data, terrain, target):
    """Strict vertical interpolation at each node, then same-run time interpolation."""
    gh = data["gh"]
    nt, nl, nn = gh.shape
    valid_times = [aware(str(v)) for v in data["valid_times_utc"]]
    if nt not in (1, 2) or valid_times != sorted(set(valid_times)):
        raise ValueError("Unexpected source time dimension")
    if (nt == 1 and valid_times[0] != target) or (nt == 2 and not valid_times[0] < target < valid_times[1]):
        raise ValueError("Source times do not exactly cover requested forecast target")
    if np.any(~np.isfinite(terrain)):
        raise ValueError("Unknown terrain in fixed sampling assets")
    output = np.full((len(ALTITUDES), nn, 3), np.nan)
    masks = {name: np.zeros((len(ALTITUDES), nn), dtype=bool) for name in MASK_NAMES}
    bad_height = (~np.isfinite(gh).all(axis=(0, 1)) |
                  ~(np.diff(gh, axis=1) > 0).all(axis=(0, 1)))
    for a, feet in enumerate(ALTITUDES):
        height = feet * .3048
        lower = np.sum(gh <= height, axis=1) - 1
        inside = (lower >= 0) & (lower < nl - 1)
        index = np.clip(lower, 0, nl - 2)[:, None, :]
        low_h = np.take_along_axis(gh, index, axis=1)[:, 0, :]
        high_h = np.take_along_axis(gh, index + 1, axis=1)[:, 0, :]
        with np.errstate(divide="ignore", invalid="ignore"):
            alpha = (height - low_h) / (high_h - low_h)
        remaining = np.ones(nn, dtype=bool)
        conditions = [("target_at_or_below_terrain", height <= terrain),
                      ("source_height_nonfinite", bad_height),
                      ("outside_vertical_range", ~inside.all(axis=0)),
                      ("lower_bracket_below_terrain", (low_h < terrain[None, :]).any(axis=0))]
        values = []
        for key in ("u", "v", "t"):
            lo = np.take_along_axis(data[key], index, axis=1)[:, 0, :]
            hi = np.take_along_axis(data[key], index + 1, axis=1)[:, 0, :]
            values.append(lo + (hi - lo) * alpha)
        values = np.stack(values, axis=-1)
        conditions += [("source_wind_nonfinite", ~np.isfinite(values[:, :, :2]).all(axis=(0, 2))),
                       ("source_temperature_nonfinite", ~np.isfinite(values[:, :, 2]).all(axis=0))]
        for reason, condition in conditions:
            masks[reason][a] = remaining & condition
            remaining &= ~condition
        if nt == 1:
            value = values[0]
        else:
            span = (valid_times[1] - valid_times[0]).total_seconds()
            before = (valid_times[1] - target).total_seconds()
            after = (target - valid_times[0]).total_seconds()
            # Integer hour weights retain the prior 14 JST (a + 2*b) / 3 rule.
            value = (values[0] * (before / 3600) + values[1] * (after / 3600)) / (span / 3600)
        value[~remaining] = np.nan
        value[:, :2] *= 3600 / 1852
        value[:, 2] -= 273.15
        output[a] = value
    return output, masks


def percentile(values, weights, probability=.95):
    keep = np.isfinite(values) & np.isfinite(weights) & (weights > 0)
    values, weights = values[keep], weights[keep]
    if not len(values):
        return None
    order = np.argsort(values, kind="stable")
    cumulative = np.cumsum(weights[order])
    i = np.searchsorted(cumulative, probability * cumulative[-1], side="left")
    return float(values[order[min(i, len(order) - 1)]])


def rows_from_fields(fields, masks, assets):
    from autonavlog.nav.airspeed import cas_from_tas, isa_temperature_c
    weights = assets["native_distance_weights_km"]
    leg_ids = list(assets["leg_ids"].astype(str))
    valid = np.isfinite(fields).all(axis=-1)
    length = np.einsum("ln,an->al", weights, valid)
    sums = np.einsum("ln,anc->alc", weights, np.where(valid[..., None], fields, 0))
    means = np.divide(sums, length[..., None], out=np.full_like(sums, np.nan), where=length[..., None] > 0)
    result = []
    for group in assets["groups"]:
        lid = group["representative_leg_id"]
        j = leg_ids.index(lid)
        leg = assets["inventory"][lid]
        support = weights[j] > 0
        w = weights[j, support]
        assigned = group["assigned_ids"]
        rows = []
        for a, altitude in enumerate(ALTITUDES):
            good = valid[a, support]
            covered = float(length[a, j])
            fraction = min(1., covered / float(assets["leg_lengths_km"][j]))
            reason_counts = {n + "_count": int(masks[n][a, support].sum()) for n in MASK_NAMES}
            reason_lengths = {n + "_distance_km": float(w[masks[n][a, support]].sum()) for n in MASK_NAMES}
            nonfinite_count = sum(reason_counts[n + "_count"] for n in MASK_NAMES[3:])
            row = {"altitude_ft_msl": altitude, "representative_leg_id": lid,
                "temperature_c": None, "u_kt": None, "v_kt": None, "cas_kt": None, "tas_kt": None,
                "fuel_flow_us_gal_per_hour": None, "fuel_per_minute_us_gal": None,
                "fuel_per_minute_us_gal_display": None,
                "wind_from_true_deg": None, "wind_speed_kt": None,
                "wind_from_true_deg_rounded_10": None, "wind_from_true_display": None,
                "wind_speed_kt_rounded": None, "leg_vector_rms_kt": None,
                "leg_vector_error_p95_kt": None, "leg_temperature_rms_c": None,
                "leg_temperature_absolute_error_p95_c": None,
                "native_count": int(support.sum()), "valid_native_count": int(good.sum()),
                "excluded_native_count": int((~good).sum()), "leg_length_km": float(assets["leg_lengths_km"][j]),
                "valid_distance_km": covered, "valid_distance_fraction": fraction,
                "spread_flag": False, "substitution_flag": False,
                "partial_flag": 0 < fraction < 1 - 1e-10,
                "max_known_leg_difference_kt": None, "worst_known_leg_id": None,
                "known_leg_difference_kt_by_id": {}, "known_leg_difference_exceedance_ids": [],
                "mask_reason_counts": reason_counts, "mask_reason_distances_km": reason_lengths,
                "source_acquisition_failures": 0, "source_nonfinite_count": nonfinite_count,
                "performance_warnings": []}
            if covered <= 0:
                terrain_count = (reason_counts["target_at_or_below_terrain_count"] +
                                 reason_counts["lower_bracket_below_terrain_count"])
                row["status"] = "terrain" if terrain_count == int(support.sum()) else "missing"
                rows.append(row)
                continue
            u, v, temperature = map(float, means[a, j])
            nw = w[good] / covered
            vector_error = np.linalg.norm(fields[a, support][good, :2] - [u, v], axis=-1)
            temp_error = fields[a, support][good, 2] - temperature
            rms = math.sqrt(float(np.dot(nw, vector_error ** 2)))
            performance = assets["policy"].select(altitude, temperature - isa_temperature_c(altitude),
                distance_nm=0, true_course_deg=0, wind_direction_deg_from=None, wind_speed_kt=0)
            tas, gph = float(performance.row.ktas), float(performance.row.gph)
            direction = math.degrees(math.atan2(-u, -v)) % 360
            rounded = (10 * math.floor((direction + 5) / 10)) % 360 or 360
            differences = {known: (float(np.linalg.norm(means[a, leg_ids.index(known), :2] - [u, v]))
                                   if np.isfinite(means[a, leg_ids.index(known), :2]).all() else None)
                           for known in assigned}
            finite_differences = {k: v for k, v in differences.items() if v is not None}
            worst = max(finite_differences, key=finite_differences.get)
            maximum = finite_differences[worst]
            exceedance = [k for k, d in finite_differences.items() if d > 5.0]
            row.update(status="valid", temperature_c=temperature, u_kt=u, v_kt=v,
                tas_kt=tas, cas_kt=float(cas_from_tas(tas, altitude, temperature)),
                fuel_flow_us_gal_per_hour=gph, fuel_per_minute_us_gal=gph / 60,
                fuel_per_minute_us_gal_display=format((Decimal(str(gph)) / Decimal(60)).quantize(
                    Decimal("0.001"), rounding=ROUND_HALF_UP), ".3f"),
                wind_from_true_deg=direction, wind_speed_kt=math.hypot(u, v),
                wind_from_true_deg_rounded_10=rounded, wind_from_true_display=f"{rounded:03d}",
                wind_speed_kt_rounded=int(round(math.hypot(u, v))),
                leg_vector_rms_kt=rms, leg_vector_error_p95_kt=percentile(vector_error, nw),
                leg_temperature_rms_c=math.sqrt(float(np.dot(nw, temp_error ** 2))),
                leg_temperature_absolute_error_p95_c=percentile(np.abs(temp_error), nw),
                spread_flag=bool(rms > 5), substitution_flag=bool(maximum > 5),
                max_known_leg_difference_kt=maximum, worst_known_leg_id=worst,
                known_leg_difference_kt_by_id=differences,
                known_leg_difference_exceedance_ids=exceedance,
                known_leg_comparison_missing_ids=[k for k, d in differences.items() if d is None],
                performance_warnings=list(performance.warnings))
            rows.append(row)
        result.append({"id": group["id"], "name_ja": "疑似セクター " + group["id"],
                       "representative_leg_id": lid, "representative_leg_label": leg["label"],
                       "representative_midpoint_lon_lat": group["midpoint_lon_lat"],
                       "known_leg_ids_mapped_by_midpoint": assigned,
                       "native_count": int(support.sum()), "rows": rows})
    return result


def build_forecast(data, source, target, cutoff, assets, requested_as_of=None):
    model = source["model"]
    initial = aware(source["run_initial_time_utc"])
    if aware(str(data["run_initial_time_utc"].item())) != initial:
        raise ValueError("Subset and manifest initial times disagree")
    recorded_times = [aware(str(t)) for t in data["valid_times_utc"]]
    expected_times = [initial + timedelta(hours=int(h)) for h in data["forecast_hours"]]
    if recorded_times != expected_times:
        raise ValueError("Subset forecast hours and valid times disagree")
    if initial > cutoff or initial > datetime.now(UTC):
        raise ValueError("Refusing a future or post-cutoff initial run")
    profiles, spatial_method = project_to_fixed_nodes(data, assets["xy"], model)
    profiles["valid_times_utc"] = data["valid_times_utc"]
    fields, masks = interpolate(profiles, assets["terrain_m"], target)
    groups = rows_from_fields(fields, masks, assets)
    source_times = [aware(str(t)) for t in data["valid_times_utc"]]
    if nt := len(source_times):
        time_weights = ([1.] if nt == 1 else [
            (source_times[1] - target).total_seconds() / (source_times[1] - source_times[0]).total_seconds(),
            (target - source_times[0]).total_seconds() / (source_times[1] - source_times[0]).total_seconds()])
    rows = [r for group in groups for r in group["rows"]]
    sources = source.get("sources", [source.get("source")])
    sources = [s for s in sources if s]
    metadata = {
        "target_date": target.astimezone(JST).date().isoformat(),
        "valid_time_jst": target.astimezone(JST).strftime("%H:%M"),
        "target_valid_time_jst": target.astimezone(JST).isoformat(),
        "target_valid_time_utc": target.isoformat(),
        "initial_time_jst": initial.astimezone(JST).strftime("%Y-%m-%d %H:%M"),
        "initial_time_utc": initial.isoformat(),
        "generated_time_jst": datetime.now(JST).isoformat(timespec="seconds"),
        "model": model, "status": "FORECAST_GENERATED", "sector_count": len(groups),
        "primary_sector_count": len(groups), "pseudo_sector_geographic_weather_meaning": False,
        "fixed_route_lookup_present": False, "grouping_source": "weather_assets/selection_10.json",
        "grouping_source_sha256": digest(assets["directory"] / "selection_10.json"),
        "representative": "Exact full representative-leg strict-valid distance-weighted u/v and temperature means; pseudo-sector area and other assigned legs do not enter this mean.",
        "lookup_method": "Find the pseudo-sector containing the planned leg midpoint, then read that one table.",
        "as_of_cutoff_utc": cutoff.isoformat(),
        "requested_as_of_utc": (requested_as_of or cutoff).isoformat(),
        "publication_deadline_jst": datetime.combine(target.astimezone(JST).date(),
            time(5 if target.astimezone(JST).hour == 9 else 10), JST).isoformat(),
        "source_url": sources[0]["url"] if sources else None,
        "source_sha256": sources[0]["sha256"] if sources else None,
        "source_urls": [s["url"] for s in sources], "source_records": sources,
        "subset_sha256": source["subset"]["sha256"],
        "source_forecast_hours": list(map(int, data["forecast_hours"])),
        "source_valid_times_utc": [t.isoformat() for t in source_times],
        "source_time_interpolation_weights": time_weights,
        "source_spatial_interpolation": spatial_method,
        "source_nonfinite_values": source["extraction"]["missing_values"],
        "source_acquisition_failures": 0, "source_missing_forecasts": 0,
        "candidate_attempts": source.get("candidate_attempts", []),
        "fallback_used": source.get("fallback_used", False),
        "fallback_model_used": source.get("fallback_model_used", model != "MSM"),
        "acquisition_budget_seconds": source.get("acquisition_budget_seconds"),
        "acquisition_elapsed_seconds": source.get("acquisition_elapsed_seconds"),
        "msm_phase_budget_exhausted": source.get("msm_phase_budget_exhausted", False),
        "fallback_policy": "One coherent model/run per forecast; newest eligible MSM, older eligible MSM, then GSM. Missing source records reject a run; terrain exclusions are not filled.",
        "terrain_note": "Fixed JMA MSM 2017 model terrain at the adopted sample nodes. Target must be above terrain and lower pressure bracket at/above terrain at every time endpoint. GSM uses the same fixed terrain guard; this is not a claim of GSM native or actual terrain clearance.",
        "spread_flag_definition": "Representative-leg distance-weighted vector RMS exceeds 5 kt.",
        "spread_flag_threshold_kt": 5., "spread_flag_threshold_status": "PROVISIONAL_DISPLAY_TRIAL_NOT_USER_ACCEPTED_LIMIT",
        "substitution_flag_definition": "Maximum vector difference to a known leg mapped to this table exceeds 5 kt at this valid time and altitude.",
        "substitution_flag_threshold_kt": 5., "substitution_flag_scope": "Known 31 legs only; no arbitrary-leg error guarantee.",
        "partial_flag_definition": "Some representative-leg distance is excluded by strict terrain/source validity.",
        "wind_direction_display": "Nearest 10 degrees half-up after component averaging; north 360.",
        "performance": assets["performance_source"]["conditions"],
        "performance_source_commit": assets["performance_source"]["commit_sha"],
        "performance_content_fingerprint": assets["performance_fingerprint"],
        "performance_status": "ATTACHED_PINNED_NAVMATE_SR22_CRUISE",
        "fuel_unit": "US gal/min", "fuel_rate_definition": "Pinned NavMate row.gph / 60; cruise only.",
        "fuel_rate_display": "0.001 US gal/min decimal half-up after preserving NavMate 0.1 GPH rounding.",
        "status_counts": dict(Counter(r["status"] for r in rows)),
        "partial_row_count": sum(r["partial_flag"] for r in rows)}
    return {"metadata": metadata, "groups": groups}


def generate_forecast(day, hour, *, as_of=None, cache=HERE / ".weather-cache",
                      assets=None, budget_seconds=230, models=("MSM", "GSM")):
    """Public single-slot API for an external scheduled job.

    `day` is the target JST calendar date, `hour` is 9 or 14. The returned object
    is one item in the established payload's `forecasts` array. All-source
    failure raises WeatherUnavailable with source attempts, never old data.
    An aware `as_of` can tighten the cutoff; it cannot extend today's deadline.
    """
    if isinstance(day, str):
        day = date.fromisoformat(day)
    if hour not in (9, 14):
        raise ValueError("Forecast hour must be 9 or 14 JST")
    now = datetime.now(UTC)
    if isinstance(as_of, str):
        as_of = aware(as_of)
    if as_of is not None and as_of.tzinfo is None:
        raise ValueError("as_of must include a UTC offset")
    requested = as_of.astimezone(UTC) if as_of is not None else now
    target = datetime.combine(day, time(hour), JST).astimezone(UTC)
    deadline = datetime.combine(day, time(5 if hour == 9 else 10), JST).astimezone(UTC)
    cutoff = min(now, requested, deadline)
    assets = load_assets() if assets is None else assets
    data, source = acquire(target, cutoff, cache, budget_seconds, models)
    return build_forecast(data, source, target, cutoff, assets, requested)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, default=None)
    parser.add_argument("--hours", nargs="+", type=int, choices=(9, 14), default=[9, 14])
    parser.add_argument("--as-of", type=aware, help="Timezone-aware cutoff; clamped to now and each publication deadline")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=HERE / ".weather-cache")
    parser.add_argument("--assets", type=Path, default=ASSETS)
    parser.add_argument("--budget-seconds", type=int, default=230)
    parser.add_argument("--models", nargs="+", choices=("MSM", "GSM"), default=["MSM", "GSM"],
                        help="Default MSM-first; GSM-only exists for operational fallback verification")
    args = parser.parse_args()
    now = datetime.now(UTC)
    day = args.date or now.astimezone(JST).date()
    requested = args.as_of or now
    assets = load_assets(args.assets)
    forecasts = []
    for hour in sorted(set(args.hours)):
        target = datetime.combine(day, time(hour), JST).astimezone(UTC)
        deadline = datetime.combine(day, time(5 if hour == 9 else 10), JST).astimezone(UTC)
        cutoff = min(now, requested, deadline)
        try:
            data, source = acquire(target, cutoff, args.cache, args.budget_seconds, tuple(args.models))
        except WeatherUnavailable as error:
            failure = {"schema": "kyushu-daily-weather-failure-1", "status": "UNAVAILABLE",
                       "target_valid_time_jst": target.astimezone(JST).isoformat(),
                       "as_of_cutoff_utc": cutoff.isoformat(),
                       "checked_at_utc": datetime.now(UTC).isoformat(),
                       "candidate_attempts": error.attempts}
            failure_path = args.output.with_suffix(".failure.json")
            write_json(failure_path, failure)
            print(json.dumps({"stage": "failed", "failure_manifest": str(failure_path)}), flush=True)
            return 2
        forecast = build_forecast(data, source, target, cutoff, assets, requested)
        forecasts.append(forecast)
        print(json.dumps({"stage": "forecast_complete", "target": target.astimezone(JST).isoformat(),
                          "model": source["model"], "initial_time_utc": source["run_initial_time_utc"],
                          "status_counts": forecast["metadata"]["status_counts"],
                          "fallback_used": source["fallback_used"]}), flush=True)
    payload = {"schema": "pseudo-kyushu-paper-forecast-1", "primary_sector_count": 10,
               "grouping_source": "weather_assets/selection_10.json", "forecasts": forecasts}
    write_json(args.output, payload)
    print(json.dumps({"stage": "complete", "forecasts": len(forecasts),
                      "output": str(args.output), "sha256": digest(args.output)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
