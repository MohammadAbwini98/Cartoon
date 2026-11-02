"""Gradual-transition and flash detection against synthetic ground truth."""
import os
import tempfile
import unittest

from remake_studio.preprocess import transitions as T
from remake_studio.preprocess.breakdown import analysis_pass, pick_cuts
from tests import synth

EDGE_TOL = 2   # frames


def run(path):
    scores, _ = analysis_pass(path)
    cuts = pick_cuts(scores, 0.30, 1, len(scores))
    th = T.thumbnails(path)
    flashes, drop = T.detect_flashes(th, cuts)
    gradual = T.detect_gradual(th, [c for c in cuts if c not in drop])
    return cuts, flashes, drop, gradual


class Gradual(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def check(self, scenario):
        path, truth = scenario(self.d)
        cuts, flashes, drop, got = run(path)
        self.assertEqual(len(got), len(truth["gradual"]),
                         f"{scenario.__name__}: got {[(t.start, t.end_excl, t.kind) for t in got]}")
        for t, (s, e, kind) in zip(got, truth["gradual"]):
            self.assertLessEqual(abs(t.start - s), EDGE_TOL, f"{scenario.__name__} start {t.start} vs {s}")
            self.assertLessEqual(abs(t.end_excl - e), EDGE_TOL, f"{scenario.__name__} end {t.end_excl} vs {e}")
            self.assertEqual(t.kind, kind)
            self.assertEqual(len(t.alpha), t.end_excl - t.start)
            self.assertTrue(all(0.0 <= a <= 1.0 for a in t.alpha))
        return cuts, flashes, drop

    # positives
    def test_static_dissolve(self):
        self.check(synth.static_dissolve)

    def test_moving_dissolve(self):
        self.check(synth.moving_dissolve)

    def test_long_dissolve_23976(self):
        self.check(synth.long_dissolve_23976)

    def test_fade_out_then_in(self):
        self.check(synth.fade_out_in)

    # negatives: motion and in-place change must not be reported
    def test_slow_pan_is_not_a_transition(self):
        self.check(synth.slow_pan)

    def test_zoom_is_not_a_transition(self):
        self.check(synth.zoom)

    def test_moving_object_is_not_a_transition(self):
        self.check(synth.moving_object)

    # flashes
    def test_flash_returning_to_same_shot_drops_both_cuts(self):
        cuts, flashes, drop = self.check(synth.flash_same_shot)
        self.assertEqual(cuts, [20, 22])
        self.assertEqual([(f["start_frame"], f["end_frame_excl"], f["returns_to_same_shot"]) for f in flashes],
                         [(20, 22, True)])
        self.assertEqual(drop, {20, 22})

    def test_flash_into_new_shot_keeps_cuts(self):
        cuts, flashes, drop = self.check(synth.flash_into_new_shot)
        self.assertEqual(cuts, [20, 22])
        self.assertEqual(flashes[0]["returns_to_same_shot"], False)
        self.assertEqual(drop, set())


class RampFit(unittest.TestCase):
    def test_vectorised_matches_brute_force(self):
        import numpy as np
        rng = np.random.default_rng(0)

        def brute(alpha):
            n = len(alpha); best = np.inf; arg = None
            c0 = np.concatenate([[0], np.cumsum(alpha ** 2)]); c1 = np.concatenate([[0], np.cumsum((1 - alpha) ** 2)])
            for s in range(n - 1):
                for e in range(s + 2, n + 1):
                    r = (np.arange(e - s) + 1) / (e - s + 1)
                    c = c0[s] + ((alpha[s:e] - r) ** 2).sum() + c1[n] - c1[e]
                    if c < best - 1e-12:
                        best, arg = c, (s, e)
            return arg

        for _ in range(200):
            n = int(rng.integers(5, 50)); s = int(rng.integers(0, n - 3)); e = int(rng.integers(s + 2, n + 1))
            a = np.zeros(n); a[e:] = 1; a[s:e] = (np.arange(e - s) + 1) / (e - s + 1)
            a = np.clip(a + rng.normal(0, rng.uniform(0, 0.2), n), 0, 1)
            self.assertEqual(T._ramp_fit(a), brute(a))


if __name__ == "__main__":
    unittest.main()
