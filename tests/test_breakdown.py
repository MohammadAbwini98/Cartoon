"""End-to-end breakdown on a synthetic episode with known structure."""
import contextlib
import io
import json
import os
import tempfile
import unittest

from remake_studio.preprocess import breakdown
from tests import synth


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Episode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.d = cls._tmp.name

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def run_breakdown(self, crf):
        src, truth = synth.episode(self.d, crf=crf)
        out = os.path.join(self.d, f"out_{crf}")
        size_before = os.path.getsize(src)
        with contextlib.redirect_stdout(io.StringIO()):
            breakdown.main([src, out])
        self.assertEqual(os.path.getsize(src), size_before, "source must be untouched")
        shots = load(os.path.join(out, "shots.json"))
        return shots, truth, out

    def check(self, crf):
        shots, truth, out = self.run_breakdown(crf)
        # partition: contiguous, frame-exact, covers every frame
        self.assertEqual(shots[0]["start_frame"], 0)
        self.assertEqual(shots[-1]["end_frame_excl"], truth["frames"])
        for a, b in zip(shots, shots[1:]):
            self.assertEqual(a["end_frame_excl"], b["start_frame"])
        # hard cuts exact; the flash inside the bars shot leaves no boundary
        starts = {r["start_frame"] for r in shots if r["kind"] == "shot"}
        for c in truth["cuts"]:
            self.assertIn(c, starts, f"crf {crf}: cut {c} missing")
        for f0, f1, _ in truth["flashes"]:
            self.assertNotIn(f0, {r["start_frame"] for r in shots})
        # gradual transitions within ±2 frames, right kind
        trans = [r for r in shots if r["kind"] in ("dissolve", "fade_in", "fade_out")]
        self.assertEqual(len(trans), len(truth["gradual"]), f"crf {crf}: {[(r['start_frame'], r['end_frame_excl'], r['kind']) for r in trans]}")
        for r, (s, e, kind) in zip(trans, truth["gradual"]):
            self.assertLessEqual(abs(r["start_frame"] - s), 2)
            self.assertLessEqual(abs(r["end_frame_excl"] - e), 2)
            self.assertEqual(r["kind"], kind)
            self.assertEqual(len(r["transition"]["alpha"]), r["frames"])
        # the 3-frame shot survives and is flagged only if shorter than 3
        three = [r for r in shots if r["start_frame"] == 48]
        self.assertEqual(three[0]["frames"], 3)
        # artifacts
        for name in ("source.json", "signal.json", "scene_scores.csv", "shots.csv", "transitions.json",
                     "manifest.json", "contact_sheet_01.jpg"):
            self.assertTrue(os.path.exists(os.path.join(out, name)), name)
        m = load(os.path.join(out, "manifest.json"))
        self.assertEqual(len(m["shots"]), len(shots))
        self.assertEqual(m["source"]["frames_decoded"], truth["frames"])
        for r in shots:
            for k in ("first", "mid", "last"):
                self.assertTrue(os.path.exists(os.path.join(out, r["keyframes"][k])))

    def test_episode_near_lossless(self):
        self.check("8")

    def test_episode_compressed_crf28(self):
        self.check("28")


class Safety(unittest.TestCase):
    def test_refuses_output_dir_containing_source(self):
        with tempfile.TemporaryDirectory() as d:
            src = synth.seg(os.path.join(d, "x.mp4"), "smptebars", 10)
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                breakdown.main([src, d])

    def test_missing_source(self):
        with self.assertRaises(SystemExit):
            breakdown.main(["/nonexistent/x.mp4", "/tmp/never"])


if __name__ == "__main__":
    unittest.main()
