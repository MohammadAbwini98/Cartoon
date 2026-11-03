"""Benchmark staging, temporal metrics and compatibility evidence."""
import tempfile
import unittest

import numpy as np

from remake_studio.hybrid import benchmark as B
from remake_studio.hybrid.scheduler import Compat


class Bench(unittest.TestCase):
    def test_stages_are_gated(self):
        self.assertEqual(B.next_stage({})["id"], "B0")
        self.assertEqual(B.next_stage({"B0": "succeeded", "B1": "succeeded"})["id"], "B2")
        self.assertIsNone(B.next_stage({"B0": "succeeded", "B1": "failed"}), "stop after a failure")
        self.assertEqual(max(s["frames"] for s in B.STAGES), 96, "never the full episode")

    def test_temporal_metrics(self):
        rng = np.random.default_rng(1)
        src = np.repeat(rng.random((1, 72, 128), dtype=np.float32), 24, axis=0)   # a held drawing...
        src[12:] += 0.2                                                          # ...then one change
        same = B.temporal_metrics(src.copy(), src)
        self.assertAlmostEqual(same["flicker_ratio"], 1.0, places=3)
        self.assertEqual(same["hold_instability"], 0.0)
        self.assertAlmostEqual(same["motion_correlation"], 1.0, places=3)
        boiling = B.temporal_metrics(src + rng.normal(0, 0.05, src.shape).astype(np.float32), src)
        self.assertGreater(boiling["flicker_ratio"], 3)
        self.assertGreater(boiling["hold_instability"], 0.03)

    def test_only_successful_runs_within_memory_validate(self):
        c = Compat(tempfile.mkdtemp())
        st = B.STAGES[2]
        ok = {"job_id": "m_1", "state": "succeeded", "metrics": {"output_frames": 48, "peak_system_used_mb": 11500,
                                                                  "peak_comfyui_rss_mb": 9000, "sec_per_frame": 8.1}}
        r = B.record(c, st, ok, None, 512, 288)
        self.assertEqual((r["status"], r["max_frames_tested"], r["peak_memory_gb"]), ("validated", 48, 11.23))
        self.assertIsNone(r["quality_equivalent"], "quality needs the side-by-side review")
        oom = {"job_id": "m_2", "state": "failed", "error_kind": "oom", "error": "MPS backend out of memory",
               "metrics": {"output_frames": 0, "peak_system_used_mb": 17900}}
        r = B.record(c, B.STAGES[3], oom, None, 512, 288)
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["failure"]["kind"], "oom")
        self.assertEqual(r["max_frames_tested"], 48, "the earlier validated length is kept")
        big = dict(ok, job_id="m_3", metrics=dict(ok["metrics"], peak_system_used_mb=16000))
        self.assertEqual(B.record(c, st, big, None, 512, 288)["status"], "failed", "over the 14.4 GB limit")


if __name__ == "__main__":
    unittest.main()
