"""Offline transaction tests for coherent Pages snapshots and bounded recovery."""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from production import pages_publication as p
from production.build_pages import build_pages, main as pages_main


class PagesPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kyushu-pages-test-")
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 11, 22, 4, 12, tzinfo=p.JST)
        self.serial = 0

    def tearDown(self):
        self.temp.cleanup()

    def package(self, *, slot="0900", model="MSM", now=None, initial=None, partial=False,
                weather_generated=None, missing=False):
        now = now or self.now
        self.serial += 1
        output = self.root / f"renderer-{self.serial}"
        output.mkdir()
        day = now.date().isoformat()
        initial = initial or now.replace(hour=0, minute=0, second=0, microsecond=0)
        image_generated = now - timedelta(seconds=1)
        acquired = weather_generated or now - timedelta(seconds=2)
        deadline = now.replace(hour=5 if slot == "0900" else 10, minute=0, second=0, microsecond=0)
        metadata = {"target_date": day, "valid_time_jst": f"{p.common.SLOTS[slot]:02d}:00",
                    "model": model, "initial_time_utc": initial.astimezone(p.UTC).isoformat(),
                    "generated_time_jst": acquired.isoformat(),
                    "as_of_cutoff_utc": min(acquired, deadline).astimezone(p.UTC).isoformat(),
                    "source_acquisition_failures": 0, "source_missing_forecasts": 0}
        groups = []
        for gid in "ABCDEFGHIJ":
            groups.append({"id": gid, "rows": [
                {"altitude_ft_msl": alt, "status": "valid", "temperature_c": 4., "cas_kt": 140.,
                 "tas_kt": 155., "wind_from_true_deg": 270., "wind_speed_kt": 12.,
                 "fuel_per_minute_us_gal": .25, "fuel_per_minute_us_gal_display": "0.250",
                 "partial_flag": False, "source_acquisition_failures": 0, "source_nonfinite_count": 0,
                 "mask_reason_counts": {"outside_vertical_range_count": 0}}
                for alt in p.common.ALTITUDES]})
        if partial:
            row = groups[0]["rows"][-1]
            row["partial_flag"] = True
            row["mask_reason_counts"]["lower_bracket_below_terrain_count"] = 1
        if missing:
            groups[0]["rows"][-1]["status"] = "missing"
        weather = {"schema": "pseudo-kyushu-paper-forecast-1", "forecasts": [{"metadata": metadata, "groups": groups}]}
        records = {}
        for kind, body in (
            ("png", b"\x89PNG\r\nfixture-" + model.encode() + now.isoformat().encode()),
            ("pdf", b"%PDF-1.7 fixture-" + model.encode() + now.isoformat().encode()),
            ("weather_json", p.common.json_bytes(weather)),
        ):
            name = (f"weather_{slot}.json" if kind == "weather_json" else f"sheet_{slot}.{kind}")
            (output / name).write_bytes(body)
            records[kind] = p.descriptor(name, body, p.ASSET_MEDIA[kind])
        entry = {"slot": slot, "valid_date": day, "valid_time_jst": metadata["valid_time_jst"],
                 "valid_at_jst": now.replace(hour=p.common.SLOTS[slot], minute=0, second=0, microsecond=0).isoformat(),
                 "model": model, "initial_time_jst": initial.isoformat(), "initial_time_utc": metadata["initial_time_utc"],
                 "weather_generated_time_jst": acquired.isoformat(), "image_generated_time_jst": image_generated.isoformat(),
                 "historical": False, "target_date_matches_generation_date": True,
                 "coverage": {"total_rows": 90, "valid_weather_rows": 90, "complete_numeric_rows": 90,
                              "terrain_rows": 0, "missing_rows": 0, "partial_rows": int(partial),
                              "all_nonterrain_rows_complete": True},
                 "files": {"png": records["png"], "pdf": records["pdf"]}}
        manifest = {"schema": "kyushu-daily-publication-1", "target_date": day, "timezone": "Asia/Tokyo",
                    "forecasts": [entry], "forecast_json": records["weather_json"]}
        path = output / "manifest.json"
        path.write_bytes(p.common.json_bytes(manifest))
        return path

    def initial_site(self, *, now=None, model="MSM"):
        now = now or self.now
        output = self.root / f"site-{self.serial}"
        build_pages([self.package(now=now, model=model), self.package(now=now, slot="1400")],
                    output, initialize_empty=True, now=now)
        return output

    def reader(self, directory, mutate=None):
        calls = []

        def read(path, limit):
            calls.append((path, limit))
            target = directory / path
            if not target.is_file():
                raise p.MissingRemote(path)
            body = target.read_bytes()
            return mutate(path, body, calls) if mutate else body

        read.calls = calls
        return read

    def state(self, directory):
        return {str(path.relative_to(directory)): p.common.sha256(path.read_bytes())
                for path in directory.rglob("*") if path.is_file()}

    @contextmanager
    def passing_time(self, start):
        elapsed = 0.

        def advance(seconds):
            nonlocal elapsed
            elapsed += seconds

        with patch.object(p, "monotonic", side_effect=lambda: elapsed), \
                patch.object(p, "sleep", side_effect=advance) as sleeper, \
                patch.object(p.common, "utc_now", side_effect=lambda: start + timedelta(seconds=elapsed)):
            yield sleeper

    def test_initial_pending_is_explicit_and_has_real_status_contract(self):
        output = self.root / "pending"
        with self.assertRaises(p.common.InvalidPackage):
            build_pages([], output, now=self.now)
        result = build_pages([], output, initialize_empty=True, now=self.now)
        self.assertEqual(result["slots"], {"0900": None, "1400": None})
        self.assertEqual(p.local_snapshot(output, now=self.now).pointers, {})
        config = p.bounded_json(output / "config.json")
        self.assertEqual(config["static_base_url"], "/")
        self.assertEqual(config["available_slots"], [])
        self.assertTrue((output / "404.html").is_file())

    def test_complete_two_slot_candidate_has_coherent_immutable_files(self):
        output = self.initial_site()
        snapshot = p.local_snapshot(output, now=self.now)
        self.assertEqual(set(snapshot.pointers), set(p.SLOTS))
        self.assertEqual(len(snapshot.files), 11)
        for slot, pointer in snapshot.pointers.items():
            self.assertEqual(pointer["quality"], "complete")
            self.assertIsNone(pointer["deadline_met"])
            self.assertNotIn("published_at_jst", pointer)
            for record in pointer["files"].values():
                self.assertTrue(record["path"].startswith(f"releases/2026-11-22/{slot}/"))
                self.assertEqual(p.common.sha256((output / record["path"]).read_bytes()), record["sha256"])

    def test_day_rollover_keeps_previous_other_slot_and_true_dates(self):
        previous = self.initial_site()
        previous14 = (previous / "latest/1400.json").read_bytes()
        next_day = self.now + timedelta(days=1)
        output = self.root / "rollover"
        result = build_pages([self.package(now=next_day)], output, previous_dir=previous, now=next_day)
        self.assertEqual(result["slots"]["0900"]["valid_date"], "2026-11-23")
        self.assertEqual(result["slots"]["1400"]["valid_date"], "2026-11-22")
        self.assertEqual((output / "latest/1400.json").read_bytes(), previous14)
        p.local_snapshot(output, now=next_day)

    def test_invalid_new_weather_preserves_existing_candidate_exactly(self):
        previous = self.initial_site()
        state = self.state(previous)
        later = self.now + timedelta(minutes=5)
        with self.assertRaises(p.common.InvalidPackage):
            build_pages([self.package(now=later, missing=True)], previous, previous_dir=previous, now=later)
        self.assertEqual(self.state(previous), state)

    def test_bounded_restore_and_post_upload_readback_validate_every_digest(self):
        public = self.initial_site()
        read = self.reader(public)
        restored = self.root / "restored"
        result = p.restore_previous("https://nav-test.pages.dev", restored, reader=read, now=self.now)
        self.assertLessEqual(len(read.calls), p.MAX_FETCHES)
        self.assertEqual(result["deployment_id"], p.bounded_json(public / "status.json")["deployment_id"])
        for slot in p.SLOTS:
            self.assertEqual((restored / f"latest/{slot}.json").read_bytes(), (public / f"latest/{slot}.json").read_bytes())
        verified = p.verify_published("https://nav-test.pages.dev", public, reader=self.reader(public), now=self.now)
        self.assertTrue(verified["verified"])
        self.assertEqual(verified["files_verified"], 11)

    def test_restore_digest_failure_does_not_replace_previous_local_snapshot(self):
        public = self.initial_site()
        destination = self.root / "prior"
        p.restore_previous("https://nav-test.pages.dev", destination, reader=self.reader(public), now=self.now)
        before = self.state(destination)
        corrupted = self.reader(public, lambda path, body, _: body + b"!" if path.endswith(".png") else body)
        with self.assertRaises(p.common.InvalidPackage):
            p.restore_previous("https://nav-test.pages.dev", destination, reader=corrupted, now=self.now)
        self.assertEqual(self.state(destination), before)

    def test_missing_referenced_file_cannot_be_treated_as_initial_empty(self):
        public = self.initial_site()
        pointer = p.bounded_json(public / "latest/1400.json")
        (public / pointer["files"]["pdf"]["path"]).unlink()
        with self.assertRaises(p.RestoreError):
            p.restore_previous("https://nav-test.pages.dev", self.root / "bad", allow_empty=True,
                               reader=self.reader(public), now=self.now)
        self.assertFalse((self.root / "bad").exists())

    def test_status_404_requires_explicit_initial_permission(self):
        def missing(path, limit):
            raise p.MissingRemote(path)
        with self.assertRaises(p.MissingRemote):
            p.restore_previous("https://nav-test.pages.dev", self.root / "none", reader=missing, now=self.now)
        result = p.restore_previous("https://nav-test.pages.dev", self.root / "empty", reader=missing,
                                    allow_empty=True, now=self.now)
        self.assertEqual(result["slots"], {"0900": None, "1400": None})

    def test_deployment_change_during_restore_fails_instead_of_combining_generations(self):
        public = self.initial_site()
        def changed(path, body, calls):
            if path == "status.json" and sum(key == path for key, _ in calls) > 1:
                return body + b" "
            return body
        with self.assertRaises(p.common.InvalidPackage):
            p.restore_previous("https://nav-test.pages.dev", self.root / "raced", reader=self.reader(public, changed), now=self.now)

    def test_gsm_to_terrain_partial_msm_is_an_allowed_preference_improvement(self):
        previous = self.initial_site(model="GSM")
        later = self.now + timedelta(minutes=5)
        result = build_pages([self.package(now=later, partial=True)], self.root / "preferred",
                             previous_dir=previous, now=later)
        self.assertEqual(result["slots"]["0900"]["model"], "MSM")
        self.assertEqual(result["slots"]["0900"]["quality"], "partial")

    def test_old_run_or_same_model_coverage_downgrade_is_rejected(self):
        previous = self.initial_site()
        later = self.now + timedelta(minutes=5)
        older_initial = self.now.replace(hour=0, minute=0, second=0) - timedelta(hours=3)
        for candidate in (self.package(now=later, initial=older_initial), self.package(now=later, partial=True)):
            with self.assertRaises(p.common.StalePublication):
                build_pages([candidate], self.root / "stale", previous_dir=previous, now=later)

    def test_strictly_newer_msm_run_may_have_legitimate_terrain_partial_rows(self):
        previous = self.initial_site()
        later = self.now + timedelta(minutes=5)
        newer_initial = self.now.replace(hour=3, minute=0, second=0)
        result = build_pages([self.package(now=later, initial=newer_initial, partial=True)],
                             self.root / "newer-partial", previous_dir=previous, now=later)
        self.assertEqual(result["slots"]["0900"]["initial_time_jst"], newer_initial.isoformat())
        self.assertEqual(result["slots"]["0900"]["quality"], "partial")

    def test_image_only_regeneration_cannot_refresh_worker_acquisition_timestamp(self):
        previous = self.initial_site()
        acquired = p.bounded_json(previous / "status.json")["slots"]["0900"]["generated_at_jst"]
        later = self.now + timedelta(minutes=12)
        result = build_pages([self.package(now=later, weather_generated=datetime.fromisoformat(acquired))],
                             self.root / "redraw", previous_dir=previous, now=later)
        self.assertEqual(result["slots"]["0900"]["generated_at_jst"], acquired)
        self.assertGreater(result["slots"]["0900"]["image_generated_at_jst"], acquired)

    def test_historical_bootstrap_retains_actual_day_and_does_not_claim_deadline(self):
        past = self.now - timedelta(days=1)
        output = self.root / "historical"
        result = build_pages([], output, bootstrap_manifest=self.package(now=past), now=self.now)
        self.assertEqual(result["slots"]["0900"]["valid_date"], "2026-11-21")
        pointer = p.bounded_json(output / "latest/0900.json")
        self.assertIsNone(pointer["deadline_met"])
        self.assertEqual(pointer["image_generated_time_jst"], (past - timedelta(seconds=1)).isoformat())

    def test_deadline_claim_waits_for_actual_public_readback(self):
        prepared = self.now.replace(hour=4, minute=59)
        public = self.initial_site(now=prepared)
        report = p.verify_published("https://nav-test.pages.dev", public, reader=self.reader(public),
                                    now=prepared.replace(hour=5, minute=3))
        self.assertFalse(report["deadline_met"]["0900"])
        self.assertEqual(report["verified_publication_at_jst"], "2026-11-22T05:03:00+09:00")

    def test_readback_rejects_an_old_deployment_before_downloading_its_assets(self):
        public = self.initial_site()
        later = self.now + timedelta(minutes=5)
        candidate = self.root / "candidate"
        build_pages([self.package(now=later)], candidate, previous_dir=public, now=later)
        reader = self.reader(public)
        with self.assertRaises(p.common.InvalidPackage):
            p.verify_published("https://nav-test.pages.dev", candidate, reader=reader, now=later)
        self.assertEqual(len(reader.calls), 1)

    def test_verify_cli_waits_for_expected_status_then_records_actual_completion(self):
        previous = self.initial_site()
        prepared = self.now.replace(hour=4, minute=59, second=58)
        candidate = self.root / "candidate"
        build_pages([self.package(now=prepared)], candidate, previous_dir=previous, now=prepared)
        old_status = (previous / "status.json").read_bytes()
        reader = self.reader(candidate, lambda path, body, calls: old_status if len(calls) == 1 else body)
        stdout, stderr = StringIO(), StringIO()
        argv = ["build_pages", "verify", "--origin", "https://nav-test.pages.dev",
                "--expected-dir", str(candidate), "--wait-seconds", "90"]
        with self.passing_time(prepared) as sleeper, patch.object(p, "HTTPReader", return_value=reader), \
                patch("sys.argv", argv), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(pages_main(), 0)
        report = json.loads(stdout.getvalue())
        self.assertTrue(report["verified"])
        self.assertEqual(report["deployment_id"], p.bounded_json(candidate / "status.json")["deployment_id"])
        self.assertEqual(report["files_verified"], 11)
        self.assertEqual(report["verified_publication_at_jst"], "2026-11-22T05:00:03+09:00")
        self.assertFalse(report["deadline_met"]["0900"])
        self.assertEqual(reader.calls[:2], [("status.json", p.MAX_STATUS_BYTES)] * 2)
        sleeper.assert_called_once_with(5)
        self.assertIn("waiting 5s", stderr.getvalue())

    def test_wait_expires_without_downloading_a_different_deployment(self):
        previous = self.initial_site()
        later = self.now + timedelta(minutes=5)
        candidate = self.root / "candidate"
        build_pages([self.package(now=later)], candidate, previous_dir=previous, now=later)
        reader = self.reader(previous)
        with self.passing_time(later) as sleeper, redirect_stderr(StringIO()), \
                self.assertRaises(p.DeploymentMismatch) as raised:
            p.verify_published("https://nav-test.pages.dev", candidate, reader=reader, wait_seconds=12)
        self.assertEqual([call.args[0] for call in sleeper.call_args_list], [5, 5, 2])
        self.assertEqual(reader.calls, [("status.json", p.MAX_STATUS_BYTES)] * 3)
        for directory in (previous, candidate):
            self.assertIn(p.bounded_json(directory / "status.json")["deployment_id"], str(raised.exception))

    def test_wait_does_not_retry_invalid_status_or_read_errors(self):
        candidate = self.initial_site()
        tampered = p.bounded_json(candidate / "status.json")
        tampered["built_at_jst"] = (self.now - timedelta(seconds=1)).isoformat()
        cases = [b"{", b"x" * (p.MAX_STATUS_BYTES + 1), p.common.json_bytes(tampered),
                 p.MissingRemote("HTTP 404"), p.RestoreError("HTTP 403")]
        for response in cases:
            reader = Mock(side_effect=response) if isinstance(response, Exception) else Mock(return_value=response)
            with self.subTest(response=type(response).__name__), patch.object(p, "sleep") as sleeper, \
                    self.assertRaises((p.common.InvalidPackage, p.RestoreError)):
                p.verify_published("https://nav-test.pages.dev", candidate, reader=reader,
                                   now=self.now, wait_seconds=90)
            self.assertEqual(reader.call_count, 1)
            sleeper.assert_not_called()

    def test_wait_does_not_hide_corrupt_assets_after_status_converges(self):
        previous = self.initial_site()
        later = self.now + timedelta(minutes=5)
        candidate = self.root / "candidate"
        build_pages([self.package(now=later)], candidate, previous_dir=previous, now=later)

        def corrupt(path, body, calls):
            if len(calls) == 1:
                return (previous / "status.json").read_bytes()
            return body[:-1] + b"X" if path.endswith(".png") else body

        reader = self.reader(candidate, corrupt)
        with self.passing_time(later) as sleeper, redirect_stderr(StringIO()), \
                self.assertRaises(p.common.InvalidPackage):
            p.verify_published("https://nav-test.pages.dev", candidate, reader=reader, wait_seconds=90)
        sleeper.assert_called_once_with(5)
        self.assertTrue(reader.calls[-1][0].endswith(".png"))
        self.assertEqual(sum(path == "status.json" for path, _ in reader.calls), 2)

    def test_wait_does_not_retry_a_change_during_full_readback(self):
        candidate = self.initial_site()

        def changed(path, body, calls):
            if path == "status.json" and len(calls) > 1:
                return body + b" "
            return body

        reader = self.reader(candidate, changed)
        with patch.object(p, "sleep") as sleeper, self.assertRaises(p.common.InvalidPackage):
            p.verify_published("https://nav-test.pages.dev", candidate, reader=reader,
                               now=self.now, wait_seconds=90)
        sleeper.assert_not_called()
        self.assertEqual(sum(path == "status.json" for path, _ in reader.calls), 2)

    def test_wait_requires_a_bounded_integer_and_an_expected_candidate(self):
        reader = Mock()
        for seconds in (-1, 121, 1.5, True, 5):
            with self.subTest(seconds=seconds), self.assertRaises(p.common.InvalidPackage):
                p.verify_published("https://nav-test.pages.dev", reader=reader, wait_seconds=seconds)
        reader.assert_not_called()

    def test_oversized_or_path_escaped_pointer_is_rejected_before_asset_requests(self):
        public = self.initial_site()
        status = p.bounded_json(public / "status.json")
        status["slots"]["0900"]["pointer"]["bytes"] = p.MAX_POINTER_BYTES + 1
        unsigned = {key: value for key, value in status.items() if key != "deployment_id"}
        status["deployment_id"] = "pages-" + p.common.sha256(p.common.json_bytes(unsigned))[:24]
        (public / "status.json").write_bytes(p.common.json_bytes(status))
        reader = self.reader(public)
        with self.assertRaises(p.common.InvalidPackage):
            p.snapshot_from_reader(reader, now=self.now)
        self.assertEqual(len(reader.calls), 1)
        with self.assertRaises(p.common.InvalidPackage):
            p.validate_descriptor(p.descriptor("../private.json", b"{}"), limit=100)


if __name__ == "__main__":
    unittest.main()
