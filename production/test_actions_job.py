import unittest
from datetime import datetime

from production.actions_job import (
    JobError, duplicate_worker, instant, resolve_plan, worker_ready,
)


class ActionsJobTests(unittest.TestCase):
    def schedule(self, cron, created, now=None):
        return resolve_plan("schedule", {"schedule": cron}, created_at=created,
                            now=now or created, run_id=100, ref="refs/heads/main")

    def worker(self, tick="0440", **overrides):
        inputs = {"trigger": "worker", "target_date": "2026-11-22", "slot": "0900",
                  "request_id": f"worker-20261122-0900-{tick}", "publish": "true"}
        inputs.update(overrides)
        return resolve_plan("workflow_dispatch", {"inputs": inputs},
                            created_at="2026-11-22T04:40:02+09:00",
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
                "slot": "0900", "request_id": "worker-20261122-0900-0440", "publish": True}},
                created_at="2026-11-22T04:20:00+09:00", now="2026-11-22T04:20:00+09:00",
                run_id=100, ref="refs/heads/main")

    def test_worker_request_earliest_run_wins(self):
        plan = self.worker()
        title = "weather-worker/2026-11-22/0900/worker-20261122-0900-0440"
        self.assertTrue(duplicate_worker(plan, [{"id": 99, "event": "workflow_dispatch", "display_title": title}]))
        self.assertFalse(duplicate_worker(plan, [{"id": 101, "event": "workflow_dispatch", "display_title": title}]))
        self.assertFalse(duplicate_worker(plan, [{"id": 99, "event": "workflow_dispatch", "display_title": title + "-other"}]))

    def test_late_worker_requires_second_source_check(self):
        status = {"schema": "kyushu-pages-status-1", "slots": {"0900": {
            "valid_date": "2026-11-22", "model": "MSM", "quality": "complete",
            "generated_at_jst": "2026-11-22T04:03:00+09:00"}}}
        now = instant("2026-11-22T04:42:00+09:00")
        self.assertTrue(worker_ready(status, self.worker("0420"), now))
        self.assertFalse(worker_ready(status, self.worker("0440"), now))
        status["slots"]["0900"]["generated_at_jst"] = "2026-11-22T04:33:00+09:00"
        self.assertTrue(worker_ready(status, self.worker("0440"), now))
        status["slots"]["0900"]["model"] = "GSM"
        self.assertFalse(worker_ready(status, self.worker("0440"), now))

    def test_schedule_never_skips_latest_source_selection(self):
        plan = self.schedule("3,33 19 * * *", "2026-11-22T04:33:00+09:00")
        status = {"schema": "kyushu-pages-status-1", "slots": {"0900": {
            "valid_date": "2026-11-22", "model": "MSM", "quality": "complete",
            "generated_at_jst": "2026-11-22T04:33:00+09:00"}}}
        self.assertFalse(worker_ready(status, plan, instant("2026-11-22T04:34:00+09:00")))

    def test_manual_both_is_explicit_and_does_not_claim_publication(self):
        plan = resolve_plan("workflow_dispatch", {"inputs": {"slot": "both", "publish": "false"}},
                            created_at="2026-11-22T15:00:00+09:00", now="2026-11-22T15:00:00+09:00",
                            run_id=100, ref="refs/heads/main")
        self.assertEqual(plan.slots, ["0900", "1400"])
        self.assertFalse(plan.publish)


if __name__ == "__main__":
    unittest.main()
