"""GitHub-hosted generation and coherent Cloudflare Pages candidate assembly.

This module does not deploy or hold provider credentials. The workflow serializes
all publication jobs, then uses Wrangler only after this candidate is complete.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
JST = timezone(timedelta(hours=9))
SLOTS = {"0900": 9, "1400": 14}
SCHEDULES = {"3,33 19 * * *": "0900", "3,33 0 * * *": "1400"}
WORKER_ID = re.compile(r"worker-(\d{8})-(0900|1400)-(\d{4})\Z")
WORKER_TICKS = {
    "0900": frozenset(['0420','0440','0500','0520','0540','0600','0620']),
    "1400": frozenset(['0920','0940','1000','1020','1040','1100']),
}
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


class JobError(ValueError):
    """A bounded, user-readable failure without provider credentials."""


def instant(value: str | datetime) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise JobError("A valid timestamp with timezone is required") from exc
    if parsed.tzinfo is None:
        raise JobError("Timezone is required")
    return parsed.astimezone(JST)


def day(value: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise JobError("target_date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise JobError("target_date must be YYYY-MM-DD")
    return value


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class JobPlan:
    schema: str
    target_date: str
    slots: list[str]
    trigger: str
    operation: str
    request_id: str | None
    run_id: int
    created_at_jst: str
    publish: bool
    skip_reason: str | None = None


def resolve_plan(event_name: str, event: dict, *, created_at: str | datetime,
                 now: str | datetime, run_id: int, ref: str) -> JobPlan:
    now, created = instant(now), instant(created_at)
    if ref != "refs/heads/main":
        raise JobError("Publication is restricted to the main branch")
    if created > now + timedelta(seconds=60) or created.date() != now.date():
        raise JobError("A queued event from another JST day cannot publish today's edition")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
        raise JobError("A verified GitHub run ID is required")
    request_id = None
    operation = "refresh"
    if event_name == "schedule":
        cron = event.get("schedule")
        if cron not in SCHEDULES:
            raise JobError("Unrecognized schedule")
        slots = [SCHEDULES[cron]]
        target_date = created.date().isoformat()
        trigger, publish = "schedule", True
        start_hour = 4 if slots[0] == "0900" else 9
        # GitHub may create the run hours after the cron's nominal window.
        # The cron selects the edition; creation still binds it to this JST day.
        if created < created.replace(hour=start_hour, minute=3, second=0, microsecond=0):
            raise JobError("Scheduled event was created before its forecast occurrence")
    elif event_name == "workflow_dispatch":
        inputs = event.get("inputs") or {}
        if not isinstance(inputs, dict):
            raise JobError("Invalid workflow inputs")
        trigger = inputs.get("trigger", "manual")
        if trigger not in {"manual", "worker"}:
            raise JobError("Unrecognized trigger")
        operation = inputs.get("operation", "refresh")
        if operation not in {"refresh", "initialize"}:
            raise JobError("Unrecognized operation")
        slot = inputs.get("slot", "both")
        if slot not in {*SLOTS, "both"}:
            raise JobError("Invalid forecast slot")
        slots = list(SLOTS) if slot == "both" else [slot]
        target_date = day(inputs.get("target_date") or now.date().isoformat())
        raw_publish = inputs.get("publish", "false")
        if raw_publish not in (True, False, "true", "false"):
            raise JobError("publish must be a boolean")
        publish = raw_publish in (True, "true")
        request_id = inputs.get("request_id") or None
        if trigger == "worker":
            if operation != "refresh" or len(slots) != 1 or not publish:
                raise JobError("Worker dispatch must publish one refreshed slot")
            match = WORKER_ID.fullmatch(request_id or "")
            if not match:
                raise JobError("Invalid Worker occurrence ID")
            compact_date, id_slot, tick = match.groups()
            if compact_date != target_date.replace("-", "") or id_slot != slot:
                raise JobError("Worker occurrence and forecast identity differ")
            if tick not in WORKER_TICKS[slot]:
                raise JobError("Worker occurrence is outside the approved schedule")
            occurrence = instant(f"{target_date}T{tick[:2]}:{tick[2:]}:00+09:00")
            if occurrence > created:
                raise JobError("Worker occurrence is future-dated")
        elif request_id:
            raise JobError("Manual runs do not accept a Worker occurrence ID")
    else:
        raise JobError("Only schedule and workflow_dispatch can generate a publication")
    if target_date != now.date().isoformat():
        raise JobError("The target must remain the current JST day")
    return JobPlan("kyushu-actions-job-1", target_date, slots, trigger, operation,
                   request_id, run_id, created.isoformat(), publish)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def github_get(repository: str, suffix: str, *, token: str) -> dict:
    if not REPOSITORY.fullmatch(repository) or not token:
        raise JobError("GitHub repository and workflow token are required")
    request = Request(f"https://api.github.com/repos/{repository}/{suffix}", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2026-03-10", "User-Agent": "kyushu-nav-weather-actions/1",
    })
    try:
        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            body = response.read(524289)
            if len(body) > 524288:
                raise JobError("GitHub response exceeded its bounded size")
    except HTTPError as exc:
        raise JobError(f"GitHub metadata request failed (HTTP {exc.code})") from None
    except URLError:
        raise JobError("GitHub metadata could not be read") from None
    try:
        result = json.loads(body)
    except (ValueError, UnicodeError):
        raise JobError("GitHub metadata was not valid JSON") from None
    if not isinstance(result, dict):
        raise JobError("GitHub metadata was not an object")
    return result


def duplicate_worker(plan: JobPlan, runs: list[dict]) -> bool:
    if plan.trigger != "worker":
        return False
    expected = f"weather-worker/{plan.target_date}/{plan.slots[0]}/{plan.request_id}"
    return any(isinstance(run.get("id"), int) and run["id"] < plan.run_id
               and run.get("event") == "workflow_dispatch"
               and run.get("display_title") == expected for run in runs)


def worker_history(repository: str, target_date: str, *, token: str) -> list[dict]:
    """Read a complete bounded day; partial or shifting pages cannot admit work."""
    start = instant(target_date + "T00:00:00+09:00").astimezone(timezone.utc)
    runs, ids = [], set()
    total = None
    for page in range(1, 11):
        query = urlencode({"event": "workflow_dispatch", "branch": "main",
                           "created": ">=" + start.isoformat().replace("+00:00", "Z"),
                           "per_page": 30, "page": page})
        history = github_get(repository, "actions/workflows/weather-daily.yml/runs?" + query, token=token)
        count, batch = history.get("total_count"), history.get("workflow_runs")
        if total is None:
            total = count
        if (type(count) is not int or not 0 <= count <= 300 or count != total
                or not isinstance(batch, list) or len(batch) != min(30, total - len(runs))):
            raise JobError("Worker request history was incomplete")
        for run in batch:
            if (not isinstance(run, dict) or type(run.get("id")) is not int or run["id"] <= 0
                    or run["id"] in ids or not isinstance(run.get("display_title"), str)
                    or run.get("event") != "workflow_dispatch" or run.get("head_branch") != "main"):
                raise JobError("Worker request history was inconsistent")
            ids.add(run["id"])
        runs.extend(batch)
        if len(runs) == total:
            return runs
    raise JobError("Worker request history was incomplete")


def worker_ready(status: dict, plan: JobPlan, now: datetime) -> bool:
    if plan.trigger != "worker" or status.get("schema") != "kyushu-pages-status-1":
        return False
    entry = (status.get("slots") or {}).get(plan.slots[0])
    if not isinstance(entry, dict) or entry.get("valid_date") != plan.target_date:
        return False
    if entry.get("model") != "MSM" or entry.get("quality") != "complete":
        return False
    hour = 4 if plan.slots[0] == "0900" else 9
    minute = 0 if (plan.request_id or "").endswith(("0420", "0920")) else 25
    threshold = instant(f"{plan.target_date}T{hour:02d}:{minute:02d}:00+09:00")
    try:
        generated = instant(entry["generated_at_jst"])
    except (KeyError, JobError):
        return False
    return threshold <= generated <= instant(now) + timedelta(seconds=60)


def read_plan(path: Path) -> JobPlan:
    value = json.loads(path.read_text())
    if value.get("schema") != "kyushu-actions-job-1":
        raise JobError("Unrecognized prepared job")
    plan = JobPlan(**value)
    if plan.target_date != datetime.now(JST).date().isoformat():
        raise JobError("Prepared job has expired")
    return plan


def output_values(path: str | None, values: dict) -> None:
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            value = str(value)
            if "\n" in value or "\r" in value:
                raise JobError("Workflow output cannot contain line breaks")
            handle.write(f"{key}={value}\n")


def prepare(args) -> dict:
    run_id = int(os.environ.get("GITHUB_RUN_ID", "0"))
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    current = github_get(repository, f"actions/runs/{run_id}", token=token)
    if current.get("id") != run_id or current.get("head_branch") != "main":
        raise JobError("GitHub run identity could not be verified")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    event_name = os.environ["GITHUB_EVENT_NAME"]
    if current.get("event") != event_name:
        raise JobError("GitHub run event does not match this invocation")
    plan = resolve_plan(event_name, event, created_at=current["created_at"],
                        now=datetime.now(JST), run_id=run_id, ref=os.environ.get("GITHUB_REF", ""))
    if plan.trigger == "worker":
        attempt = current.get("run_attempt")
        if type(attempt) is not int or attempt < 1:
            raise JobError("GitHub run attempt could not be verified")
        runs = worker_history(repository, plan.target_date, token=token)
        if not any(run["id"] == plan.run_id for run in runs):
            raise JobError("Current Worker run is absent from request history")
        if attempt > 1 or duplicate_worker(plan, runs):
            plan = JobPlan(**{**asdict(plan), "skip_reason": "duplicate_worker_occurrence"})
    write_json(args.output, asdict(plan))
    output_values(args.github_output, {"skip": str(bool(plan.skip_reason)).lower(),
                                      "publish": str(plan.publish).lower(),
                                      "target_date": plan.target_date})
    return asdict(plan)


def generate(args) -> dict:
    from production.weather_pipeline import generate_forecast
    from production.render_daily import render_daily
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    forecast = generate_forecast(day=args.date, hour=SLOTS[args.slot],
                                 as_of=datetime.now(JST), cache=directory / "cache", budget_seconds=230)
    weather = directory / "weather.json"
    write_json(weather, {"schema": "pseudo-kyushu-paper-forecast-1",
                         "primary_sector_count": 10, "forecasts": [forecast]})
    render_daily(weather, directory / "rendered", required_date=args.date)
    return {"status": "rendered", "target_date": args.date, "slot": args.slot,
            "manifest": str(directory / "rendered/manifest.json"), "published": False}


def build_candidate(args) -> dict:
    from production.pages_publication import restore_previous
    from production.build_pages import build_pages
    plan = read_plan(args.plan)
    if plan.skip_reason:
        result = {"status": plan.skip_reason, "changed": False, "published": False}
    elif plan.operation == "initialize":
        # Explicit setup only. A running site must never be replaced with an empty one.
        if args.origin:
            raise JobError("Initialize only an unconfigured Pages project; do not supply an existing origin")
        built = build_pages([], args.output, automatic_updates_enabled=False, initialize_empty=True)
        result = {"status": "initialized_candidate", "changed": True, "published": False, "build": built}
    else:
        if not args.origin:
            raise JobError("A previously initialized Pages origin is required")
        with tempfile.TemporaryDirectory(prefix="kyushu-actions-") as temporary:
            directory = Path(temporary)
            previous = directory / "previous"
            restore_previous(args.origin, previous)
            status = json.loads((previous / "status.json").read_text())
            if worker_ready(status, plan, datetime.now(JST)):
                result = {"status": "already_current", "changed": False, "published": False}
            else:
                manifests = []
                for slot in plan.slots:
                    if datetime.now(JST).date().isoformat() != plan.target_date:
                        raise JobError("Generation crossed into the next JST day")
                    work = directory / slot
                    command = [sys.executable, "-m", "production.actions_job", "generate",
                               "--date", plan.target_date, "--slot", slot, "--directory", str(work)]
                    try:
                        subprocess.run(command, cwd=ROOT, check=True, timeout=320,
                                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                    except subprocess.TimeoutExpired:
                        raise JobError("Acquisition/rendering exceeded its time budget") from None
                    except subprocess.CalledProcessError:
                        raise JobError("Acquisition or rendering failed; published files are unchanged") from None
                    manifests.append(work / "rendered/manifest.json")
                if datetime.now(JST).date().isoformat() != plan.target_date:
                    raise JobError("Candidate date expired before publication")
                built = build_pages(manifests, args.output, previous_dir=previous,
                                    automatic_updates_enabled=args.automatic_updates)
                result = {"status": "candidate_ready", "changed": True, "published": False,
                          "target_date": plan.target_date, "slots": plan.slots,
                          "previous_deployment_id": status.get("deployment_id"), "build": built}
    write_json(args.report, result)
    output_values(args.github_output, {"changed": str(result["changed"]).lower()})
    return result


def predeploy(args) -> dict:
    """Recheck date, complete candidate, and unchanged base immediately before upload."""
    from production.pages_publication import HTTPReader, local_snapshot, validate_status, MAX_STATUS_BYTES
    plan = read_plan(args.plan)
    report = json.loads(args.report.read_text())
    if not plan.publish or not report.get("changed"):
        raise JobError("This job did not prepare an authorized publication")
    snapshot = local_snapshot(args.directory, include_frontend=True)
    if snapshot.status["deployment_id"] != report["build"]["deployment_id"]:
        raise JobError("Candidate identity changed after assembly")
    if plan.operation == "refresh":
        for slot in plan.slots:
            entry = snapshot.status["slots"].get(slot)
            if not entry or entry["valid_date"] != plan.target_date:
                raise JobError("The candidate has the wrong target date")
        if not args.origin:
            raise JobError("The existing publication origin is required")
        current = json.loads(HTTPReader(args.origin)("status.json", MAX_STATUS_BYTES))
        validate_status(current)
        if current["deployment_id"] != report.get("previous_deployment_id"):
            raise JobError("Public files changed after restore; rebuild before deploying")
    read_plan(args.plan)  # A network request may have crossed the JST date boundary.
    return {"status": "ready_for_upload", "deployment_id": snapshot.status["deployment_id"],
            "target_date": plan.target_date, "published": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--github-output")
    g = commands.add_parser("generate")
    g.add_argument("--date", type=day, required=True)
    g.add_argument("--slot", choices=SLOTS, required=True)
    g.add_argument("--directory", type=Path, required=True)
    b = commands.add_parser("build")
    b.add_argument("--plan", type=Path, required=True)
    b.add_argument("--origin")
    b.add_argument("--output", type=Path, required=True)
    b.add_argument("--report", type=Path, required=True)
    b.add_argument("--automatic-updates", action="store_true")
    b.add_argument("--github-output")
    check = commands.add_parser("predeploy")
    check.add_argument("--plan", type=Path, required=True)
    check.add_argument("--report", type=Path, required=True)
    check.add_argument("--directory", type=Path, required=True)
    check.add_argument("--origin")
    args = parser.parse_args()
    try:
        result = {"prepare": prepare, "generate": generate, "build": build_candidate,
                  "predeploy": predeploy}[args.command](args)
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    except (JobError, OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc), "published": False}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
