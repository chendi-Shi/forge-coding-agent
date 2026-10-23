"""Small deterministic checks for what the concurrency probe actually proves."""

import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_SPEC = spec_from_file_location("load_test", Path(__file__).resolve().parents[1] / "scripts" / "load_test.py")
assert _SPEC and _SPEC.loader
load_test = module_from_spec(_SPEC)
_SPEC.loader.exec_module(load_test)


class LoadMetricsTests(unittest.TestCase):
    def test_peak_counts_overlap_not_total_submissions(self):
        self.assertEqual(load_test.peak_running([(0, 5), (1, 4), (3, 6), (6, 7)]), 3)
        self.assertEqual(load_test.peak_running([(0, 1), (1, 2), (2, 3)]), 1)
        self.assertEqual(load_test.peak_running([(1, 1), (1, 1)]), 0)
        self.assertEqual(load_test.peak_running([]), 0)

    def test_peak_rejects_invalid_or_non_finite_timestamps(self):
        for interval in ((2, 1), (float("nan"), 3), (1, float("inf"))):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                load_test.peak_running([interval])

    def test_output_requires_matching_probe_and_latest_assistant_message(self):
        state = {
            "values": {
                "probe": {"started_at": 1.0, "finished_at": 2.0, "instance": "worker-a", "request_id": "probe-unique"},
                "messages": [
                    {"role": "user", "content": "probe-unique"},
                    {"type": "ai", "content": "Completed probe-unique"},
                ],
            }
        }
        self.assertEqual(load_test.validate_state(state, "probe-unique")["instance"], "worker-a")
        state["values"]["messages"][-1]["content"] = "Completed another-request"
        with self.assertRaisesRegex(load_test.ProbeError, "OUTPUT_REQUEST_ID_MISMATCH"):
            load_test.validate_state(state, "probe-unique")
        state["values"]["messages"][-1]["content"] = "probe-unique"
        state["values"]["probe"]["request_id"] = "other"
        with self.assertRaisesRegex(load_test.ProbeError, "PROBE_REQUEST_ID_MISMATCH"):
            load_test.validate_state(state, "probe-unique")

    def test_success_alone_does_not_pass_peak_requirement(self):
        runs = [
            {
                "request_id": f"probe-{i}",
                "status": "success",
                "end_to_end_seconds": float(i + 1),
                "probe": {"started_at": float(i), "finished_at": float(i + 1), "instance": "worker-a"},
            }
            for i in range(3)
        ]
        summary = load_test.summarize(runs, min_peak=3)
        self.assertEqual(summary["success"], 3)
        self.assertEqual(summary["peak_running"], 1)
        self.assertFalse(summary["passed"])
        self.assertEqual(summary["p50_end_to_end_seconds"], 2)
        self.assertAlmostEqual(summary["p95_end_to_end_seconds"], 2.9)

    def test_failures_fail_report_even_when_peak_is_met(self):
        runs = [
            {"request_id": "one", "status": "success", "end_to_end_seconds": 2.0, "probe": {"started_at": 1.0, "finished_at": 2.0, "instance": "worker-a"}},
            {"request_id": "two", "status": "failed", "stage": "poll_run", "error": "RUN_ERROR"},
        ]
        summary = load_test.summarize(runs, min_peak=1)
        self.assertFalse(summary["passed"])
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["instances"]["worker-a"]["successful_runs"], 1)


if __name__ == "__main__":
    unittest.main()
