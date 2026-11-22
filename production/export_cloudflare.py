#!/usr/bin/env python3
"""Export the fixed, standalone Cloudflare/Actions project without research caches."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
FIXED = [
    ".python-version", ".gitignore", ".env.example", "requirements.txt",
    "kyushu_coastline.geojson", "route_reference_draft.json",
    "analysis/geographic_revision/build_geographic_sheet.py",
    "analysis/pseudo_sectors/build_pseudo_sheet.py",
    "analysis/pseudo_sectors/geometry/pseudo_sectors_10.geojson",
    "analysis/pseudo_sectors/geometry/pseudo_boundaries_10.geojson",
    "analysis/pseudo_sectors/design/selection_10.json",
    "analysis/pseudo_sectors/presentation_v2/decision.json",
    "analysis/pseudo_sectors/fonts/LiberationSans-Regular.ttf",
    "analysis/pseudo_sectors/fonts/LiberationSans-Bold.ttf",
    "analysis/pseudo_sectors/fonts/LICENSE.txt",
    "production/weather_pipeline.py", "production/weather_fetch.py", "production/render_daily.py",
    "production/publication.py", "production/pages_publication.py", "production/build_pages.py",
    "production/actions_job.py", "production/export_cloudflare.py",
    "production/test_weather_pipeline.py", "production/test_actions_job.py",
    "production/test_pages_publication.py", "production/requirements-weather.txt",
    "production/requirements-renderer.txt", "production/README.md", "production/WEATHER_PIPELINE.md",
    "production/renderer_README.md", "production/infra_reuse_notes.md", "production/deployment.json",
    "production/vendor/jma_gpv_weather-0.6.0-py3-none-any.whl",
    "output/kyushu_weather_sector_requirements.md",
    ".github/workflows/weather-daily.yml", ".github/workflows/verify.yml",
]
WORKER = ["worker.mjs", "worker.test.mjs", "wrangler.jsonc", "package.json", "package-lock.json", "README.md"]


def export(output: Path) -> dict:
    output = output.resolve()
    if output == ROOT or output == ROOT / "production" or not output.is_relative_to(ROOT / "production"):
        raise ValueError("Use a new generated directory under production")
    if (output / ".git").exists():
        raise ValueError("Do not overwrite a git checkout; use a fresh export directory")
    if output.exists():
        if not (output / ".generated-cloudflare-export").exists():
            raise ValueError("Refusing to replace an unrecognized directory")
        shutil.rmtree(output)
    output.mkdir(parents=True)
    (output / ".generated-cloudflare-export").write_text("Standalone runtime export\n")
    paths = {ROOT / name for name in FIXED}
    paths.update(ROOT / "services/weather-trigger" / name for name in WORKER)
    paths.update(path for path in (ROOT / "production/weather_assets").rglob("*")
                 if path.is_file() and "__pycache__" not in path.parts)
    paths.update((ROOT / "production/web").glob("*"))
    records = []
    for source in sorted(paths):
        name = source.relative_to(ROOT).as_posix()
        if not source.is_file() or any(part in {"__pycache__", "node_modules", ".wrangler", "cache"} for part in source.parts):
            raise ValueError(f"Unexpected runtime source: {name}")
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        body = source.read_bytes()
        target.write_bytes(body)
        records.append({"path": name, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()})
    body = (ROOT / "production/README.md").read_bytes()
    (output / "README.md").write_bytes(body)
    records.append({"path": "README.md", "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()})
    manifest = {"schema": "kyushu-cloudflare-source-1", "repository": "Yuto-24/kyushu-nav-weather",
                "files": records}
    (output / "source_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return {"directory": str(output), "files": len(records) + 1,
            "bytes": sum(record["bytes"] for record in records), "contains_credentials": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "production/cloudflare_repo")
    print(json.dumps(export(parser.parse_args().output), ensure_ascii=False))
