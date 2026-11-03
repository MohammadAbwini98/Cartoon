"""Pilot graph against node schemas; frame-exact shot assembly with original audio."""
import os
import re
import subprocess
import tempfile
import unittest

from remake_studio import assemble
from remake_studio.workflows import PilotParams, check_graph, pilot_shot_graph


class Graph(unittest.TestCase):
    def test_pilot_graph_matches_node_schemas(self):
        g = pilot_shot_graph(PilotParams("shot_0007.mp4", "remi_ref.png", frames=72))
        self.assertEqual(check_graph(g), [])
        self.assertEqual(g["16"]["inputs"]["batch_size"], 72, "one latent per source frame")
        self.assertEqual(g["2"]["inputs"]["frame_load_cap"], 72)
        self.assertEqual(g["17"]["inputs"]["denoise"], 1.0, "no source pixels carried over")
        self.assertEqual(g["19"]["class_type"], "SaveImage", "RunPod worker returns only image outputs")

    def test_checker_catches_drift(self):
        g = pilot_shot_graph(PilotParams("a.mp4", "r.png", frames=8))
        g["12"]["class_type"] = "ControlNetLoaderAdvanced"          # pre-v3 id
        g["17"]["inputs"]["scheduler"] = "karras_typo"
        del g["7"]["inputs"]["weight_type"]
        g["18"]["inputs"]["samples"] = ["17", 3]
        problems = "\n".join(check_graph(g))
        for needle in ("unknown node type ControlNetLoaderAdvanced", "karras_typo", "weight_type", "output 3 of KSampler"):
            self.assertIn(needle, problems)

    def test_rejects_bad_size(self):
        with self.assertRaises(ValueError):
            pilot_shot_graph(PilotParams("a.mp4", "r.png", frames=8, width=770))


def ff(*a):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *a], check=True)


def tone_onset(path):
    """Seconds at which the first tone starts (end of leading silence)."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", path, "-af", "silencedetect=n=-40dB:d=0.05",
                        "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"silence_end: ([\d.]+)", r.stderr)
    return float(m.group(1)) if m else None


class Assemble(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.d = self._t.name
        # 23.976 fps episode, 240 frames; a tone starts exactly at frame 100 (100/23.976 s)
        self.fps = 24000 / 1001
        self.onset = 100 / self.fps
        self.ep = os.path.join(self.d, "episode.mp4")
        ff("-f", "lavfi", "-i", "testsrc=s=320x180:r=24000/1001", "-f", "lavfi", "-i",
           f"aevalsrc='if(gte(t,{self.onset:.9f}),0.5*sin(2*PI*660*t),0)':s=48000:d=10.1",
           "-frames:v", "240", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", self.ep)

    def tearDown(self):
        self._t.cleanup()

    def frames(self, n, name="f"):
        d = os.path.join(self.d, name)
        os.makedirs(d)
        ff("-f", "lavfi", "-i", "color=c=0x3060c0:s=256x144:r=24", "-frames:v", str(n), os.path.join(d, "remake_%05d_.png"))
        return d

    def test_shot_is_frame_exact_and_audio_aligned(self):
        out = os.path.join(self.d, "shot.mp4")
        r = assemble.assemble_shot(self.frames(48), self.ep, 80, 128, out, size="512x288")
        self.assertEqual(r["probe"]["frames"], 48)
        self.assertEqual((r["probe"]["width"], r["probe"]["height"]), (512, 288))
        self.assertEqual(r["fps"], "24000/1001")
        # the tone is at episode frame 100 -> 20 frames into this shot
        expected = 20 / self.fps
        got = tone_onset(out)
        self.assertIsNotNone(got)
        self.assertLess(abs(got - expected), 0.012, f"audio offset {got:.4f}s vs {expected:.4f}s")

    def test_wrong_frame_count_is_refused(self):
        out = os.path.join(self.d, "bad.mp4")
        with self.assertRaisesRegex(assemble.AssemblyError, "exactly 48 frames"):
            assemble.assemble_shot(self.frames(47), self.ep, 80, 128, out)
        self.assertFalse(os.path.exists(out))

    def test_side_by_side_evidence(self):
        shot = os.path.join(self.d, "shot.mp4")
        assemble.assemble_shot(self.frames(48), self.ep, 80, 128, shot)
        cmp_ = assemble.compare(self.ep, 80, 128, shot, os.path.join(self.d, "cmp.mp4"), height=180)
        from remake_studio.media import validate
        p = validate.probe(cmp_)
        self.assertEqual(p["frames"], 48)
        self.assertEqual(p["height"], 180)
        self.assertEqual(p["width"], 320 + 320, "source and remake side by side")


if __name__ == "__main__":
    unittest.main()


class Variants(unittest.TestCase):
    def test_mps_and_keyframe_variants_pass_schema_and_template_allowlists(self):
        from remake_studio.hybrid import protocol as P
        from remake_studio.workflows import mps_params, variant_graph
        for template in ("pilot_shot_v1", "keyframes_v1"):
            g = variant_graph(mps_params(control_video="shot.mp4", reference_image="ref.png", frames=6), "mps", template)
            self.assertEqual(check_graph(g), [], template)
            self.assertTrue({n["class_type"] for n in g.values()} <= P.TEMPLATES[template]["nodes"], template)
            self.assertTrue(g["4"]["inputs"]["pose_estimator"].endswith(".onnx"))
            self.assertEqual((g["16"]["inputs"]["width"], g["16"]["inputs"]["height"]), (512, 288))
        kf = variant_graph(mps_params(control_video="s.mp4", reference_image="r.png", frames=6), "mps", "keyframes_v1")
        self.assertNotIn("9", kf)
        self.assertEqual(kf["17"]["inputs"]["model"], ["7", 0])
