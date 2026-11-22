#!/usr/bin/env python3
"""Render validated dated forecasts with the approved fixed A4 presentation.

This module only creates dated artifacts. A publisher decides when a result may
replace a public 0900/1400 alias, using the manifest's date and coverage fields.
The renderer never changes a weather value or relabels an old valid date.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analysis/pseudo_sectors"))
import build_pseudo_sheet as renderer

JST = timezone(timedelta(hours=9))
NUMERIC_FIELDS = (
    "temperature_c", "cas_kt", "tas_kt", "wind_from_true_deg", "wind_speed_kt",
    "fuel_per_minute_us_gal",
)
MODEL_DISPLAY = {"MSM": "MSM", "GSM": "GSM", "UNAVAILABLE": "取得不可"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def timestamp(value: str, *, allow_naive_jst: bool = False) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        if not allow_naive_jst:
            raise ValueError(f"Timestamp requires a UTC offset: {value}")
        parsed = parsed.replace(tzinfo=JST)
    return parsed.astimezone(JST)


def finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def reject_constant(value):
    raise ValueError(f"Non-finite JSON value is not permitted: {value}")


def validate(payload: dict, required_date: str | None = None) -> list[dict]:
    if payload.get("schema") != "pseudo-kyushu-paper-forecast-1":
        raise ValueError("Expected schema pseudo-kyushu-paper-forecast-1.")
    forecasts = payload.get("forecasts")
    if not isinstance(forecasts, list) or not 1 <= len(forecasts) <= 2:
        raise ValueError("Supply one or two forecasts.")
    periods = set()
    dates = set()
    selection = renderer.read(renderer.SELECTION)
    representatives = {r["sector_id"]: r["leg_id"] for r in selection["representatives"]}
    for forecast in forecasts:
        metadata = forecast["metadata"]
        target = metadata["target_date"]
        if date.fromisoformat(target).isoformat() != target:
            raise ValueError(f"Target date must be YYYY-MM-DD: {target}")
        if required_date is not None and target != required_date:
            raise ValueError(f"Requested date {required_date} differs from source date {target}.")
        valid_clock = metadata["valid_time_jst"]
        if valid_clock not in {"09:00", "14:00"}:
            raise ValueError(f"Unsupported forecast time: {valid_clock}")
        period = (target, valid_clock)
        if period in periods:
            raise ValueError(f"Duplicate forecast: {period}")
        periods.add(period)
        dates.add(target)
        valid = timestamp(f"{target}T{valid_clock}:00+09:00")
        model = metadata.get("model")
        if model not in MODEL_DISPLAY:
            raise ValueError(f"A single actual MSM/GSM source run is required; received {model!r}.")
        initial = metadata.get("initial_time_jst")
        if model != "UNAVAILABLE":
            if not initial:
                raise ValueError("A sourced forecast must have its actual initialization time.")
            initial = timestamp(initial, allow_naive_jst=True)
            if initial > valid:
                raise ValueError("Forecast initialization is later than its valid time.")
            if metadata.get("initial_time_utc"):
                if timestamp(metadata["initial_time_utc"]) != initial:
                    raise ValueError("UTC and JST initialization times disagree.")
        elif initial is not None:
            raise ValueError("Unavailable weather cannot claim a source initialization time.")
        groups = forecast["groups"]
        if len(groups) != 10 or {g["id"] for g in groups} != set(renderer.IDS):
            raise ValueError("Every forecast must contain exactly the accepted ten sectors A-J.")
        for group in groups:
            if group["representative_leg_id"] != representatives[group["id"]]:
                raise ValueError(f"Representative leg changed for sector {group['id']}.")
            if [r["altitude_ft_msl"] for r in group["rows"]] != renderer.base.ALTITUDES:
                raise ValueError("Rows must contain all nine altitudes in descending order.")
            for row in group["rows"]:
                status = row.get("status")
                if status not in {"valid", "terrain", "missing"}:
                    raise ValueError(f"Unpublishable row status: {status!r}")
                if status == "valid":
                    if model == "UNAVAILABLE":
                        raise ValueError("Unavailable weather cannot contain valid rows.")
                    for field in ("temperature_c", "wind_from_true_deg", "wind_speed_kt"):
                        if not finite(row.get(field)):
                            raise ValueError(f"Valid weather row lacks finite {field}.")
                    if row["wind_speed_kt"] < 0:
                        raise ValueError("Wind speed cannot be negative.")
                for field in NUMERIC_FIELDS:
                    if row.get(field) is not None and not finite(row[field]):
                        raise ValueError(f"Non-finite {field} in a row.")
                fuel = row.get("fuel_per_minute_us_gal_display")
                if fuel is not None and not isinstance(fuel, str):
                    raise ValueError("Fuel display must be an explicit decimal string or null.")
    if len(dates) != 1:
        raise ValueError("One render batch must refer to one target date.")
    return sorted(forecasts, key=lambda f: f["metadata"]["valid_time_jst"])


def coverage(forecast: dict) -> dict:
    rows = [row for group in forecast["groups"] for row in group["rows"]]
    statuses = Counter(row["status"] for row in rows)
    fractions = [row.get("valid_distance_fraction") for row in rows if row["status"] == "valid"]
    fractions = [value for value in fractions if finite(value)]
    numeric_complete = sum(row["status"] == "valid" and
                           all(finite(row.get(field)) for field in NUMERIC_FIELDS) and
                           bool(row.get("fuel_per_minute_us_gal_display")) for row in rows)
    return {
        "total_rows": len(rows),
        "valid_weather_rows": statuses["valid"],
        "complete_numeric_rows": numeric_complete,
        "terrain_rows": statuses["terrain"],
        "missing_rows": statuses["missing"],
        "partial_rows": sum(bool(row.get("partial_flag")) for row in rows),
        "spread_rows": sum(bool(row.get("spread_flag")) for row in rows),
        "substitution_rows": sum(bool(row.get("substitution_flag")) for row in rows),
        "minimum_valid_distance_fraction": min(fractions) if fractions else None,
        "all_nonterrain_rows_complete": numeric_complete + statuses["terrain"] == len(rows),
        "per_sector": {
            group["id"]: dict(Counter(row["status"] for row in group["rows"]))
            for group in forecast["groups"]
        },
    }


def file_record(path: Path, media_type: str) -> dict:
    return {"path": path.name, "media_type": media_type, "bytes": path.stat().st_size,
            "sha256": digest(path)}


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def render_daily(input_path: Path, output_dir: Path, *, generated_at: str | None = None,
                 required_date: str | None = None, qa: bool = False) -> dict:
    input_path, output_dir = input_path.resolve(), output_dir.resolve()
    payload = json.loads(input_path.read_text(encoding="utf-8"), parse_constant=reject_constant)
    forecasts = validate(payload, required_date)
    generated = timestamp(generated_at) if generated_at else datetime.now(JST)
    generated = generated.replace(microsecond=0)
    generated_at = generated.isoformat()
    target_date = forecasts[0]["metadata"]["target_date"]
    date_tag = target_date.replace("-", "")
    hours_tag = "_".join(f["metadata"]["valid_time_jst"].replace(":", "") for f in forecasts)
    output_dir.mkdir(parents=True, exist_ok=True)
    shapes, _ = renderer.inputs(input_path)
    refs = renderer.read(ROOT / "route_reference_draft.json")
    # Rendering and validation complete before the publication manifest changes.
    with tempfile.TemporaryDirectory(prefix=".render-", dir=output_dir) as staging_name:
        staging = Path(staging_name)
        reports = []
        entries = []
        source_json = staging / f"kyushu_pseudo_forecasts_{date_tag}_{hours_tag}.json"
        shutil.copyfile(input_path, source_json)
        for forecast in forecasts:
            metadata = forecast["metadata"]
            model = metadata["model"]
            printable = {**forecast, "metadata": {**metadata, "model": MODEL_DISPLAY[model]}}
            report = renderer.render(printable, shapes, refs, output_dir=staging,
                                     weather_path=input_path, generated_at=generated_at,
                                     qa_dir=staging / "qa" if qa else None)
            report["source_weather_metadata"] = metadata
            for kind in ("pdf", "png"):
                report[kind] = str(output_dir / Path(report[kind]).name)
            reports.append(report)
            initial = metadata.get("initial_time_jst")
            initial_iso = timestamp(initial, allow_naive_jst=True).isoformat() if initial else None
            entries.append({
                "slot": metadata["valid_time_jst"].replace(":", ""),
                "valid_date": metadata["target_date"],
                "valid_time_jst": metadata["valid_time_jst"],
                "valid_at_jst": f"{target_date}T{metadata['valid_time_jst']}:00+09:00",
                "model": model,
                "initial_time_jst": initial_iso,
                "initial_time_utc": timestamp(initial_iso).astimezone(timezone.utc).isoformat() if initial_iso else None,
                "weather_generated_time_jst": metadata.get("generated_time_jst"),
                "image_generated_time_jst": generated_at,
                "historical": report["historical_label"],
                "target_date_matches_generation_date": target_date == generated.date().isoformat(),
                "coverage": coverage(forecast),
                "files": {
                    "png": file_record(staging / Path(report["png"]).name, "image/png"),
                    "pdf": file_record(staging / Path(report["pdf"]).name, "application/pdf"),
                },
                "source_metadata": metadata,
            })
        combined = None
        if len(reports) == 2:
            combined_path = staging / f"kyushu_pseudo_weather_{date_tag}.pdf"
            with renderer.fitz.open() as doc:
                for report in reports:
                    with renderer.fitz.open(staging / Path(report["pdf"]).name) as single:
                        doc.insert_pdf(single)
                doc.set_metadata({"title": f"九州 上空風・気温 {target_date} 09:00 / 14:00 JST"})
                doc.save(combined_path)
            combined = file_record(combined_path, "application/pdf")
        layout_path = staging / f"layout_checks_{date_tag}_{hours_tag}.json"
        write_json(layout_path, {"schema": "pseudo-print-layout-2", "forecasts": reports,
                                 "combined_pdf": str(output_dir / combined["path"]) if combined else None})
        manifest = {
            "schema": "kyushu-daily-publication-1",
            "target_date": target_date,
            "timezone": "Asia/Tokyo",
            "generated_time_jst": generated_at,
            "forecasts": entries,
            "forecast_json": file_record(source_json, "application/json"),
            "layout_report": file_record(layout_path, "application/json"),
            "combined_pdf": combined,
            "input_sha256": digest(input_path),
            "fixed_design_sha256": {
                str(path.relative_to(ROOT)): digest(path)
                for path in (renderer.GEOMETRY, renderer.BOUNDARIES, renderer.SELECTION)
            },
            "rendering": {"paper_mm": [297, 210], "orientation": "landscape", "dpi": 300,
                          "color": "grayscale", "pages_per_forecast": 1,
                          "altitudes_descending_ft_msl": renderer.base.ALTITUDES},
        }
        dated_manifest = staging / f"manifest_{date_tag}_{hours_tag}.json"
        write_json(dated_manifest, manifest)
        write_json(staging / "manifest.json", manifest)
        # Move the generic manifest last; it only describes this completed batch.
        for path in sorted(staging.iterdir(), key=lambda p: p.name == "manifest.json"):
            if path.is_dir():
                shutil.copytree(path, output_dir / path.name, dirs_exist_ok=True)
            else:
                os.replace(path, output_dir / path.name)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Forecast JSON produced by weather_pipeline.py")
    parser.add_argument("--output-dir", required=True, type=Path, help="Directory for dated PNG/PDF/JSON artifacts")
    parser.add_argument("--generated-at", help="Timezone-aware image generation timestamp (default: now)")
    parser.add_argument("--require-date", help="Fail if input target date differs from this YYYY-MM-DD")
    parser.add_argument("--qa", action="store_true", help="Also save 150 dpi previews in output-dir/qa")
    args = parser.parse_args()
    manifest = render_daily(args.input, args.output_dir, generated_at=args.generated_at,
                            required_date=args.require_date, qa=args.qa)
    print(json.dumps({"manifest": str(args.output_dir.resolve() / "manifest.json"),
                      "target_date": manifest["target_date"],
                      "forecasts": [{"slot": f["slot"], "model": f["model"],
                                     "initial_time_jst": f["initial_time_jst"],
                                     "coverage": f["coverage"], "files": f["files"]}
                                    for f in manifest["forecasts"]]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
