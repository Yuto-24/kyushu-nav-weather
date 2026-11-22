#!/usr/bin/env python3
"""Build a coherent static Pages candidate or verify one after external upload.

Build never uploads. A workflow must serialize the full restore/generate/deploy
sequence for this site, then call verify against the fixed Pages origin. A
restoration failure aborts the candidate instead of dropping the other slot.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import tempfile

try:
    from . import pages_publication as pages
except ImportError:
    import pages_publication as pages

HERE = Path(__file__).resolve().parent
HEADERS = """/*
  X-Content-Type-Options: nosniff
  Referrer-Policy: same-origin
/
  Cache-Control: public, max-age=0, must-revalidate
/index.html
  Cache-Control: public, max-age=0, must-revalidate
/app.js
  Cache-Control: public, max-age=0, must-revalidate
/style.css
  Cache-Control: public, max-age=0, must-revalidate
/config.json
  Cache-Control: no-store
/status.json
  Cache-Control: no-store
/bootstrap/manifest.json
  Cache-Control: no-store
/latest/*
  Cache-Control: no-store
/releases/*
  Cache-Control: public, max-age=31536000, immutable
"""
NOT_FOUND = """<!doctype html><html lang="ja"><meta charset="utf-8"><title>404</title>
<p>このファイルは見つかりません。</p><p><a href="/">九州 上空風・気温</a></p></html>
"""


def _frontend(pointers: dict, *, automatic_updates_enabled: bool) -> dict[str, bytes]:
    result = {}
    for name in ("index.html", "app.js", "style.css"):
        source = HERE / "web" / name
        pages.require(source.is_file() and source.stat().st_size <= 1024 * 1024, "Viewer asset is absent or unexpectedly large")
        result[name] = source.read_bytes()
    entries = [{key: value for key, value in pointers[slot].items() if key != "source_metadata"}
               for slot in pages.SLOTS if slot in pointers]
    result["bootstrap/manifest.json"] = pages.common.json_bytes({
        "schema": "kyushu-daily-publication-1", "timezone": "Asia/Tokyo",
        "target_date": max((entry["valid_date"] for entry in entries), default=None),
        "forecasts": entries,
    })
    result["config.json"] = pages.common.json_bytes({
        "automatic_updates_enabled": bool(automatic_updates_enabled),
        "static_base_url": "/", "blob_base_url": None,
        "bootstrap_manifest_url": "/bootstrap/manifest.json",
        "available_slots": [slot for slot in pages.SLOTS if slot in pointers],
    })
    result["_headers"] = HEADERS.encode("utf-8")
    # A real 404 file prevents Pages' SPA fallback from turning missing JSON
    # paths into a successful HTML response, particularly at initialization.
    result["404.html"] = NOT_FOUND.encode("utf-8")
    return result


def build_pages(manifest_paths, output_dir: Path, *, previous_dir: Path | None = None,
                previous_origin: str | None = None, bootstrap_manifest: Path | None = None,
                automatic_updates_enabled: bool = True, now: datetime | None = None,
                allow_empty_origin: bool = False, initialize_empty: bool = False) -> dict:
    """Assemble both slots and immutable assets; commit only after complete QA.

    Missing/failed generation is represented by omitting that slot's manifest.
    Its verified prior release remains byte-for-byte unchanged. Invalid supplied
    candidates abort the build and leave any prior output directory untouched.
    Historical bootstrap data may only fill an empty slot and retain true dates.
    """
    now = pages.common.timestamp(now or pages.common.utc_now())
    manifest_paths = [Path(path) for path in manifest_paths]
    pages.require(len(manifest_paths) <= 2, "At most two renderer manifests may enter one deployment")
    pages.require(not (previous_dir and previous_origin), "Choose a local restored snapshot or a fixed origin, not both")
    pages.require(not initialize_empty or not (previous_dir or previous_origin),
                  "Explicit initialization cannot be combined with a prior publication")
    pages.require(previous_dir or previous_origin or bootstrap_manifest or initialize_empty,
                  "A previous snapshot is required; use --initialize-empty explicitly for the first site")
    pages.require(not allow_empty_origin or previous_origin, "--allow-empty-origin applies only to initial origin restoration")
    output_dir = Path(output_dir).resolve()
    pages.require(output_dir != HERE and output_dir != HERE.parent, "Output must be a generated directory")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".build-pages-", dir=output_dir.parent) as temporary:
        work = Path(temporary)
        if previous_origin:
            restored = work / "previous"
            pages.restore_previous(previous_origin, restored, allow_empty=allow_empty_origin, now=now)
            previous_dir = restored
        snapshot = pages.local_snapshot(previous_dir, now=now) if previous_dir else None
        pointers = dict(snapshot.pointers) if snapshot else {}
        files = {key: value for key, value in snapshot.files.items() if key != "status.json"} if snapshot else {}
        original_slots = set(pointers)
        updated, seeded = [], []
        packages_dir = work / "packages"
        packages_dir.mkdir()
        if bootstrap_manifest:
            for manifest, directory in pages.renderer_packages(Path(bootstrap_manifest), packages_dir):
                slot = manifest["forecasts"][0]["slot"]
                if slot in pointers:
                    continue
                pointer, release_files = pages.make_release(manifest, directory, now=now, historical_seed=True)
                pointers[slot] = pointer
                files.update(release_files)
                seeded.append(slot)
        for manifest_path in manifest_paths:
            for manifest, directory in pages.renderer_packages(manifest_path, packages_dir):
                pointer, release_files = pages.make_release(manifest, directory, now=now)
                slot = pointer["slot"]
                pages.replacement_allowed(pointers.get(slot), pointer)
                pointers[slot] = pointer
                files.update(release_files)
                updated.append(slot)
        reachable = set()
        for slot, pointer in pointers.items():
            reachable.add(f"latest/{slot}.json")
            reachable.add(pointer["manifest"]["path"])
            reachable.update(record["path"] for record in pointer["files"].values())
        files = {path: files[path] for path in sorted(reachable)}
        pages.require(sum(map(len, files.values())) <= pages.MAX_SNAPSHOT_BYTES, "Candidate exceeds its byte bound")
        frontend = _frontend(pointers, automatic_updates_enabled=automatic_updates_enabled)
        status = pages.status_for(pointers, files, now=now,
                                  frontend_hashes={path: pages.common.sha256(body) for path, body in sorted(frontend.items())})
        files.update(frontend)
        files["status.json"] = pages.common.json_bytes(status)
        staging = work / "site"
        staging.mkdir()
        pages.write_snapshot(staging, files)
        verified = pages.local_snapshot(staging, now=now)
        pages.require(verified.status == status, "Candidate status changed during local verification")
        pages.commit_directory(staging, output_dir)
    return {"directory": str(output_dir), "deployment_id": status["deployment_id"],
            "slots": status["slots"], "updated_slots": sorted(set(updated)), "seeded_slots": sorted(seeded),
            "retained_slots": sorted(original_slots - set(updated)),
            "automatic_updates_enabled": bool(automatic_updates_enabled),
            "files": len(files), "bytes": sum(map(len, files.values()))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Create a complete, locally verified deployment directory")
    build.add_argument("--manifest", action="append", type=Path, default=[])
    build.add_argument("--output", required=True, type=Path)
    prior = build.add_mutually_exclusive_group()
    prior.add_argument("--previous-dir", type=Path)
    prior.add_argument("--previous-origin")
    build.add_argument("--bootstrap-manifest", type=Path)
    build.add_argument("--initialize-empty", action="store_true", help="Explicit first deployment with no prior publication")
    build.add_argument("--allow-empty-origin", action="store_true", help="Initial-only permission for a missing status.json")
    build.add_argument("--disable-automatic-updates", action="store_true")
    build.add_argument("--now", help="Offset-aware test timestamp; omit in operational builds")
    restore = commands.add_parser("restore", help="Strictly restore the two prior public releases")
    restore.add_argument("--origin", required=True)
    restore.add_argument("--output", type=Path, required=True)
    restore.add_argument("--allow-empty-origin", action="store_true")
    verify = commands.add_parser("verify", help="Verify status/pointers/manifests/assets after external deployment")
    verify.add_argument("--origin", required=True)
    verify.add_argument("--expected-dir", type=Path)
    args = parser.parse_args()
    if args.command == "build":
        result = build_pages(args.manifest, args.output, previous_dir=args.previous_dir,
                             previous_origin=args.previous_origin, bootstrap_manifest=args.bootstrap_manifest,
                             automatic_updates_enabled=not args.disable_automatic_updates,
                             now=pages.common.timestamp(args.now) if args.now else None,
                             initialize_empty=args.initialize_empty, allow_empty_origin=args.allow_empty_origin)
    elif args.command == "restore":
        result = pages.restore_previous(args.origin, args.output, allow_empty=args.allow_empty_origin)
    else:
        result = pages.verify_published(args.origin, args.expected_dir)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
