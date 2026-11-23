import unittest
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from production import actions_job as job
from production.actions_job import (
    JobError, duplicate_worker, instant, resolve_plan, worker_ready, worker_history,
)


class ActionsJobTests(unittest.TestCase):
    def schedule(self, cron, created, now=None):
        return resolve_plan("schedule", {"schedule": cron}, created_at=created,
                            now=now or created, run_id=100, ref="refs/heads/main")

    def worker(self, tick="0335", **overrides):
        inputs = {"trigger": "worker", "target_date": "2026-11-22", "slot": "0900",
                  "request_id": f"worker-20261122-0900-{tick}", "publish": "true"}
        inputs.update(overrides)
        return resolve_plan("workflow_dispatch", {"inputs": inputs},
                            created_at="2026-11-22T03:35:02+09:00",
                            now="2026-11-22T04:42:00+09:00", run_id=100, ref="refs/heads/main")

    def test_utc_evening_schedule_uses_next_jst_day(self):
        plan = self.schedule("3,33 19 * * *", "2026-11-21T19:03:00Z")
        self.assertEqual((plan.target_date, plan.slots), ("2026-11-22", ["0900"]))
        self.assertEqual(plan.trigger, "schedule")

    def test_delayed_schedule_keeps_original_slot(self):
        plan = self.schedule("3,33 19 * * *", "2026-11-22T04:03:00+09:00", "2026-11-22T09:01:00+09:00")
        self.assertEqual(plan.slots, ["0900"])
        afternoon = self.schedule("3,33 0 * * *", "2026-11-22T09:33:00+09:00")
        self.assertEqual(afternoon.slots, ["1400"])

    def test_github_can_create_and_start_each_schedule_hours_late(self):
        for cron, slot in (("3,33 19 * * *", "0900"), ("3,33 0 * * *", "1400")):
            plan = self.schedule(cron, "2026-11-22T18:15:00+09:00", "2026-11-22T23:59:00+09:00")
            self.assertEqual((plan.target_date, plan.slots), ("2026-11-22", [slot]))
        for cron, early in (("3,33 19 * * *", "04:02"), ("3,33 0 * * *", "09:02")):
            with self.assertRaises(JobError):
                self.schedule(cron, f"2026-11-22T{early}:59+09:00")

    def test_worker_delayed_creation_and_runner_wait_are_allowed_only_same_day(self):
        for slot, tick in (("0900", "0315"), ("0900", "0335"), ("1400", "0915"), ("1400", "0935")):
            event = {"inputs": {"trigger": "worker", "target_date": "2026-11-22", "slot": slot,
                                "request_id": f"worker-20261122-{slot}-{tick}", "publish": True}}
            for created in (f"{tick[:2]}:{tick[2:]}:08", "18:00:00"):
                plan = resolve_plan("workflow_dispatch", event,
                                    created_at=f"2026-11-22T{created}+09:00", now="2026-11-22T23:59:00+09:00",
                                    run_id=100, ref="refs/heads/main")
                self.assertEqual(plan.slots, [slot])
                with self.assertRaises(JobError):
                    resolve_plan("workflow_dispatch", event, created_at=f"2026-11-22T{created}+09:00",
                                 now="2026-11-23T00:00:00+09:00", run_id=100, ref="refs/heads/main")

    def test_prior_day_queue_and_unknown_cron_rejected(self):
        with self.assertRaises(JobError):
            self.schedule("3,33 19 * * *", "2026-11-21T04:03:00+09:00", "2026-11-22T04:03:00+09:00")
        with self.assertRaises(JobError):
            self.schedule("3,33 0,19 * * *", "2026-11-22T04:03:00+09:00")

    def test_worker_id_binds_date_slot_and_publish_intent(self):
        self.assertEqual(self.worker().slots, ["0900"])
        for changes in ({"target_date": "2026-11-21"}, {"slot": "1400"},
                        {"publish": "false"}, {"operation": "initialize"},
                        {"request_id": "worker-20261122-0900-0430"}):
            with self.subTest(changes=changes), self.assertRaises(JobError):
                self.worker(**changes)

    def test_branch_and_worker_future_timestamp_rejected(self):
        with self.assertRaises(JobError):
            resolve_plan("schedule", {"schedule": "3,33 19 * * *"},
                         created_at="2026-11-22T04:03:00+09:00", now="2026-11-22T04:03:00+09:00",
                         run_id=100, ref="refs/heads/feature")
        with self.assertRaises(JobError):
            resolve_plan("workflow_dispatch", {"inputs": {"trigger": "worker", "target_date": "2026-11-22",
                "slot": "0900", "request_id": "worker-20261122-0900-0335", "publish": True}},
                created_at="2026-11-22T03:15:00+09:00", now="2026-11-22T03:15:00+09:00",
                run_id=100, ref="refs/heads/main")

    def test_worker_request_earliest_run_wins(self):
        plan = self.worker()
        title = "weather-worker/2026-11-22/0900/worker-20261122-0900-0335"
        self.assertTrue(duplicate_worker(plan, [{"id": 99, "event": "workflow_dispatch", "display_title": title}]))
        self.assertFalse(duplicate_worker(plan, [{"id": 101, "event": "workflow_dispatch", "display_title": title}]))
        self.assertFalse(duplicate_worker(plan, [{"id": 99, "event": "workflow_dispatch", "display_title": title + "-other"}]))

    def test_schedule_never_skips_latest_source_selection(self):
        plan = self.schedule("3,33 19 * * *", "2026-11-22T04:33:00+09:00")
        status = {"schema": "kyushu-pages-status-1", "slots": {"0900": {
            "valid_date": "2026-11-22", "model": "MSM", "quality": "complete",
            "generated_at_jst": "2026-11-22T04:33:00+09:00"}}}
        self.assertFalse(worker_ready(status, plan, instant("2026-11-22T04:34:00+09:00")))

    def test_worker_initial_targets_and_stage_changes(self):
        for slot, tick, initial, later, next_initial in (
                ("0900", "0015", "00:00", "03:15", "03:00"),
                ("1400", "0615", "06:00", "09:15", "09:00")):
            created = f"2026-11-22T{tick[:2]}:{tick[2:]}:00+09:00"
            plan = resolve_plan("workflow_dispatch", {"inputs": {
                "trigger": "worker", "target_date": "2026-11-22", "slot": slot,
                "request_id": f"worker-20261122-{slot}-{tick}", "publish": True,
            }}, created_at=created, now=created, run_id=100, ref="refs/heads/main")
            entry = {"valid_date": "2026-11-22", "model": "MSM", "quality": "complete",
                     "initial_time_jst": f"2026-11-22T{initial}:00+09:00",
                     "generated_at_jst": created}
            status = {"schema": "kyushu-pages-status-1", "slots": {slot: entry}}
            self.assertTrue(worker_ready(status, plan, instant(created)))
            boundary = instant(f"2026-11-22T{later}:00+09:00")
            self.assertTrue(worker_ready(status, plan, boundary - timedelta(seconds=1)))
            # Regenerating the old Run at the boundary does not satisfy the new target.
            entry["generated_at_jst"] = boundary.isoformat()
            self.assertFalse(worker_ready(status, plan, boundary))
            entry["initial_time_jst"] = f"2026-11-22T{next_initial}:00+09:00"
            self.assertTrue(worker_ready(status, plan, boundary))
            for field, value in (("model", "GSM"), ("quality", "partial"),
                                 ("valid_date", "2026-11-21"),
                                 ("initial_time_jst", "2026-11-22T23:00:00+09:00"),
                                 ("generated_at_jst", "2026-11-21T23:59:00+09:00"),
                                 ("generated_at_jst", "2026-11-22T23:59:00+09:00")):
                with self.subTest(slot=slot, field=field, value=value):
                    bad = {**status, "slots": {slot: {**entry, field: value}}}
                    self.assertFalse(worker_ready(bad, plan, boundary))
            self.assertFalse(worker_ready(status, plan, instant("2026-11-23T00:00:00+09:00")))

    def test_all_worker_occurrences_and_outside_windows(self):
        expected = {
            "0900": [f"{h:02d}{m:02d}" for h in range(6) for m in (15, 35, 55)] + ["0615"],
            "1400": [f"{h:02d}{m:02d}" for h in range(6, 11) for m in (15, 35, 55)],
        }
        for slot, ticks in expected.items():
            for tick in ticks:
                with self.subTest(slot=slot, tick=tick):
                    created = f"2026-11-22T{tick[:2]}:{tick[2:]}:00+09:00"
                    event = {"inputs": {"trigger": "worker", "target_date": "2026-11-22", "slot": slot,
                                        "request_id": f"worker-20261122-{slot}-{tick}", "publish": True}}
                    plan = resolve_plan("workflow_dispatch", event, created_at=created, now=created,
                                        run_id=100, ref="refs/heads/main")
                    self.assertEqual(plan.slots, [slot])
                    early = instant(created) - timedelta(seconds=1)
                    with self.assertRaises(JobError):
                        resolve_plan("workflow_dispatch", event, created_at=early, now=early,
                                     run_id=100, ref="refs/heads/main")
        for slot, tick in (("0900", "0000"), ("0900", "0635"), ("0900", "0420"),
                           ("1400", "0555"), ("1400", "1115"), ("1400", "0920")):
            with self.subTest(slot=slot, tick=tick), self.assertRaises(JobError):
                resolve_plan("workflow_dispatch", {"inputs": {
                    "trigger": "worker", "target_date": "2026-11-22", "slot": slot,
                    "request_id": f"worker-20261122-{slot}-{tick}", "publish": True,
                }}, created_at="2026-11-22T12:00:00+09:00", now="2026-11-22T12:00:00+09:00",
                    run_id=100, ref="refs/heads/main")

    def test_manual_both_is_explicit_and_does_not_claim_publication(self):
        plan = resolve_plan("workflow_dispatch", {"inputs": {"slot": "both", "publish": "false"}},
                            created_at="2026-11-22T15:00:00+09:00", now="2026-11-22T15:00:00+09:00",
                            run_id=100, ref="refs/heads/main")
        self.assertEqual(plan.slots, ["0900", "1400"])
        self.assertFalse(plan.publish)

    def history_run(self, run_id):
        return {"id": run_id, "event": "workflow_dispatch", "head_branch": "main",
                "display_title": "weather-manual/0900"}

    def test_worker_history_reads_later_pages_and_deduplicates(self):
        runs = [self.history_run(i + 200) for i in range(30)] + [{
            **self.history_run(99), "display_title": "weather-worker/2026-11-22/0900/worker-20261122-0900-0335"}]
        def get(repository, suffix, *, token):
            query = parse_qs(urlsplit(suffix).query)
            self.assertEqual(query["branch"], ["main"])
            self.assertEqual(query["created"], [">=2026-11-21T15:00:00Z"])
            page = int(query["page"][0])
            return {"total_count": len(runs), "workflow_runs": runs[(page - 1) * 30:page * 30]}
        with patch.object(job, "github_get", side_effect=get) as getter:
            result = worker_history("Example/repo", "2026-11-22", token="fixture")
        self.assertEqual(getter.call_count, 2)
        self.assertTrue(duplicate_worker(self.worker(), result))

    def test_worker_history_fails_closed_on_incomplete_or_changing_pages(self):
        first = [self.history_run(i + 200) for i in range(30)]
        bad_pages = [
            {"total_count": 301, "workflow_runs": first},
            {"total_count": 31, "workflow_runs": []},
            {"total_count": 32, "workflow_runs": [self.history_run(99)]},
            {"total_count": 31, "workflow_runs": [first[0]]},
            {"total_count": 31, "workflow_runs": [{**self.history_run(99), "head_branch": "other"}]},
        ]
        for bad in bad_pages:
            with self.subTest(bad=bad), patch.object(job, "github_get", side_effect=[
                {"total_count": 31, "workflow_runs": first}, bad
            ]), self.assertRaises(JobError):
                worker_history("Example/repo", "2026-11-22", token="fixture")

    def test_prepare_skips_duplicate_and_rerun_but_not_unrelated_failures(self):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 11, 22, 18, tzinfo=job.JST).astimezone(tz)
        title = "weather-worker/2026-11-22/0900/worker-20261122-0900-0335"
        for attempt, duplicate, skipped in ((1, False, False), (1, True, True), (2, False, True)):
            with self.subTest(attempt=attempt, duplicate=duplicate), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                event = root / "event.json"
                event.write_text(json.dumps({"inputs": {"trigger": "worker", "target_date": "2026-11-22",
                    "slot": "0900", "request_id": "worker-20261122-0900-0335", "publish": True}}))
                current = {**self.history_run(100), "created_at": "2026-11-22T12:00:00+09:00",
                           "run_attempt": attempt, "display_title": title}
                earlier = {**self.history_run(99), "conclusion": "failure",
                           "display_title": title if duplicate else title.replace("0335", "0315")}
                with patch.dict(job.os.environ, {"GITHUB_RUN_ID": "100", "GITHUB_REPOSITORY": "Example/repo",
                    "GITHUB_TOKEN": "fixture", "GITHUB_EVENT_PATH": str(event),
                    "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main"}), \
                    patch.object(job, "datetime", Clock), \
                    patch.object(job, "github_get", side_effect=[current,
                        {"total_count": 2, "workflow_runs": [current, earlier]}]):
                    result = job.prepare(SimpleNamespace(output=root / "plan.json", github_output=None))
                self.assertEqual(bool(result["skip_reason"]), skipped)


if __name__ == "__main__":
    unittest.main()
