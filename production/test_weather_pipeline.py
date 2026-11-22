"""Focused scientific/time integrity checks; no live network requests."""
from __future__ import annotations

from datetime import datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

import weather_fetch as fetch
import weather_pipeline as pipeline


class ForecastTimeTests(unittest.TestCase):
    def test_latest_candidates_are_never_future_and_msm_precedes_gsm(self):
        cutoff = fetch.aware("2026-11-21T19:59:00Z")
        target = fetch.aware("2026-11-22T09:00:00+09:00")
        candidates = list(fetch.candidates(target, cutoff))
        self.assertEqual(candidates[0].run, fetch.aware("2026-11-21T18:00:00Z"))
        self.assertTrue(all(c.run <= cutoff for c in candidates))
        models = [c.model for c in candidates]
        self.assertNotIn("MSM", models[models.index("GSM"):])
        self.assertTrue(all(c.valid_times[0] <= target <= c.valid_times[-1] for c in candidates))

    def test_14jst_two_chunks_preserve_same_initial_time(self):
        run = fetch.aware("2026-11-21T12:00:00Z")
        target = fetch.aware("2026-11-22T14:00:00+09:00")
        candidate = fetch.candidate_for("MSM", run, target)
        self.assertEqual(candidate.hours, (15, 18))
        self.assertEqual(len(candidate.urls), 2)
        self.assertIn("FH00-15", candidate.urls[0])
        self.assertIn("FH18-33", candidate.urls[1])
        self.assertTrue(all("20261121120000" in url for url in candidate.urls))

    def test_gsm_filename_uses_day_hour_not_hour_integer(self):
        run = fetch.aware("2026-11-21T12:00:00Z")
        candidate = fetch.candidate_for("GSM", run, run + timedelta(hours=30))
        self.assertIn("FD0103-0200", candidate.urls[0])
        self.assertEqual(candidate.hours, (30,))

    def test_future_publication_rejected_even_for_old_run(self):
        cutoff = fetch.aware("2026-11-21T19:00:00Z")
        with self.assertRaisesRegex(ValueError, "after cutoff"):
            fetch._availability({"last-modified": format_datetime(cutoff + timedelta(seconds=1))}, cutoff)
        with self.assertRaisesRegex(ValueError, "no Last-Modified"):
            fetch._availability({}, cutoff)

    def test_failed_msm_reaches_gsm_and_retains_real_target(self):
        cutoff = fetch.aware("2026-11-21T17:00:00Z")
        target = fetch.aware("2026-11-22T00:00:00Z")
        visited = []
        def fake_acquire(candidate, requested, actual_cutoff, cache, deadline):
            visited.append(candidate.model)
            self.assertEqual(requested, target)
            self.assertEqual(actual_cutoff, cutoff)
            if candidate.model == "MSM":
                raise OSError("Controlled test: MSM unavailable")
            return {"test": True}, {"model": "GSM", "run_initial_time_utc": candidate.run.isoformat()}
        with patch.object(fetch, "acquire_candidate", side_effect=fake_acquire), patch("builtins.print"):
            arrays, source = fetch.acquire(target, cutoff, "unused")
        self.assertEqual(visited[-1], "GSM")
        self.assertTrue(all(m == "MSM" for m in visited[:-1]))
        self.assertTrue(source["fallback_model_used"])
        self.assertEqual(source["candidate_attempts"][-1]["status"], "SELECTED")


class InterpolationTests(unittest.TestCase):
    def source(self):
        gh = np.broadcast_to(np.asarray([0., 1500., 4000.])[None, :, None], (2, 3, 3)).copy()
        u = np.broadcast_to(np.asarray([0., 3.])[:, None, None], (2, 3, 3)).copy()
        return {"gh": gh, "u": u, "v": np.zeros_like(u),
                "t": 280 + u,
                "valid_times_utc": np.asarray(["2026-11-22T03:00:00Z", "2026-11-22T06:00:00Z"])}

    def test_vertical_then_actual_time_weights_and_terrain_reasons(self):
        target = fetch.aware("2026-11-22T14:00:00+09:00")
        values, masks = pipeline.interpolate(self.source(), np.asarray([0., 500., 3000.]), target)
        i = pipeline.ALTITUDES.index(2500)
        self.assertAlmostEqual(values[i, 0, 0], 2 * 3600 / 1852)
        self.assertAlmostEqual(values[i, 0, 2], 282 - 273.15)
        self.assertTrue(masks["lower_bracket_below_terrain"][i, 1])
        self.assertTrue(masks["target_at_or_below_terrain"][i, 2])
        self.assertFalse(masks["source_wind_nonfinite"].any())
        self.assertTrue(np.isnan(values[i, 1:]).all())

    def test_both_time_endpoints_must_be_finite(self):
        data = self.source()
        data["u"][1, :, 0] = np.nan
        values, masks = pipeline.interpolate(data, np.zeros(3), fetch.aware("2026-11-22T05:00:00Z"))
        self.assertTrue(masks["source_wind_nonfinite"][:, 0].all())
        self.assertTrue(np.isnan(values[:, 0]).all())
        self.assertFalse(masks["target_at_or_below_terrain"].any())

    def test_exact_target_is_required_for_single_endpoint(self):
        data = {k: v[:1].copy() for k, v in self.source().items()}
        with self.assertRaisesRegex(ValueError, "exactly cover"):
            pipeline.interpolate(data, np.zeros(3), fetch.aware("2026-11-22T05:00:00Z"))

    def test_atomic_json_does_not_leave_partial_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "forecast.json"
            fetch.write_json(path, {"version": 1})
            fetch.write_json(path, {"version": 2})
            self.assertEqual(pipeline.read(path), {"version": 2})
            self.assertFalse(path.with_suffix(".json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
