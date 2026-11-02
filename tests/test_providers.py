"""Providers, budget gate and job runner against scripted HTTP servers."""
import base64
import datetime as dt
import json
import os
import subprocess
import tempfile
import unittest

from remake_studio.providers.base import Capability, Cost
from remake_studio.providers.budget import Budget, BudgetError
from remake_studio.providers.comfyui import ComfyUI
from remake_studio.providers.http import Http, HttpError
from remake_studio.providers.jobs import Runner
from remake_studio.providers.paid import FalQueue, Replicate, RunPodComfy
from remake_studio.providers.pricing import PriceTable
from tests.mock_http import MockServer

TODAY = dt.date.today().isoformat()


def tiny_mp4(path, w=64, h=48, frames=12):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc=s={w}x{h}:r=24", "-frames:v", str(frames), "-pix_fmt", "yuv420p", path], check=True)
    with open(path, "rb") as f:
        return f.read()


def nosleep(_):
    pass


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.d = self._t.name
        self.state = os.path.join(self.d, "state")
        self.out = os.path.join(self.d, "out")
        self.mp4 = tiny_mp4(os.path.join(self.d, "clip.mp4"))
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.close()
        self._t.cleanup()

    def serve(self, routes):
        s = MockServer(routes)
        self.servers.append(s)
        return s

    def authorize(self, **kw):
        a = {"authorized_by": "test", "currency": "USD", "total_cap": 1.0, "per_job_cap": 0.6,
             "providers": ["fal", "runpod", "replicate"], "scope": "test", "expires": "2999-01-01", **kw}
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "authorization.json"), "w") as f:
            json.dump(a, f)


# --------------------------------------------------------------------- ComfyUI
class ComfyUITests(Base):
    def comfy_server(self, history_after=2, error=False):
        polls = {"n": 0}
        info = {"KSampler": {}, "ADE_AnimateDiffLoaderGen1": {}, "ACN_AdvancedControlNetApply": {},
                "VHS_VideoCombine": {}, "LoadImage": {}, "SaveImage": {},
                "CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["model_a.safetensors"]]}}}}

        def history(req):
            polls["n"] += 1
            if polls["n"] < history_after:
                return 200, {}
            st = {"status_str": "error", "completed": False, "messages": [["execution_error", {"exception_message": "OOM"}]]} \
                if error else {"status_str": "success", "completed": True, "messages": []}
            return 200, {"p1": {"status": st, "outputs": {"9": {"gifs": [{"filename": "remi_00001.mp4", "subfolder": "", "type": "output"}]},
                                                          "5": {"images": [{"filename": "prev.png", "subfolder": "", "type": "temp"}]}}}}
        return self.serve({
            ("GET", "/object_info"): lambda r: (200, info),
            ("POST", "/upload/image"): lambda r: (200, {"name": "kf.png", "subfolder": "", "type": "input"}),
            ("POST", "/prompt"): lambda r: (200, {"prompt_id": "p1", "number": 1, "node_errors": {}}),
            ("GET", "/history/p1"): history,
            ("GET", "/queue"): lambda r: (200, {"queue_running": [[0, "p1"]], "queue_pending": []}),
            ("GET", "/view"): lambda r: (200, self.mp4, "video/mp4"),
        })

    def wf(self):
        return {"1": {"class_type": "LoadImage", "inputs": {"image": "kf.png"}},
                "9": {"class_type": "VHS_VideoCombine", "inputs": {}}}

    def test_discovery(self):
        s = self.comfy_server()
        c = ComfyUI(s.url)
        packs = c.node_packs()
        self.assertTrue(packs["animatediff"] and packs["advanced_controlnet"] and packs["video_helper"])
        self.assertFalse(packs["ipadapter"])
        self.assertIn("video_to_video", [x.task for x in c.capabilities()])
        self.assertEqual(c.models("checkpoints"), ["model_a.safetensors"])

    def test_end_to_end_validated(self):
        s = self.comfy_server()
        kf = os.path.join(self.d, "kf.png")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=red:s=8x8", "-frames:v", "1", kf], check=True)
        r = Runner({"comfyui": ComfyUI(s.url)}, self.state, self.out, sleep=nosleep, poll_seconds=0)
        job = r.create("comfyui", "workflow", {"workflow": self.wf(), "uploads": [kf]}, [kf],
                       expect={"width": 64, "height": 48, "frames": 12}, shot="0007")
        job = r.run(job["id"])
        self.assertEqual(job["state"], "validated", job.get("problems"))
        self.assertEqual(len(job["outputs"]), 1, "temp previews must be skipped")
        self.assertEqual(len(s.posts("/upload/image")), 1)
        self.assertTrue(os.path.exists(job["outputs"][0]["path"]))

    def test_wrong_output_is_rejected_not_accepted(self):
        s = self.comfy_server()
        r = Runner({"comfyui": ComfyUI(s.url)}, self.state, self.out, sleep=nosleep, poll_seconds=0)
        job = r.run(r.create("comfyui", "workflow", {"workflow": self.wf()}, expect={"frames": 24})["id"])
        self.assertEqual(job["state"], "rejected")
        self.assertIn("frames 12 != expected 24", job["problems"][0])

    def test_execution_error_fails(self):
        s = self.comfy_server(error=True)
        r = Runner({"comfyui": ComfyUI(s.url)}, self.state, self.out, sleep=nosleep, poll_seconds=0)
        job = r.run(r.create("comfyui", "workflow", {"workflow": self.wf()}, max_attempts=1)["id"])
        self.assertEqual(job["state"], "failed")
        self.assertIn("OOM", job["last_error"])

    def test_missing_nodes_refused_before_submit(self):
        s = self.comfy_server()
        c = ComfyUI(s.url)
        with self.assertRaisesRegex(ValueError, "IPAdapterAdvanced"):
            c.submit("workflow", {"workflow": {"1": {"class_type": "IPAdapterAdvanced", "inputs": {}}}})
        self.assertEqual(s.posts("/prompt"), [])

    def test_same_request_is_one_job(self):
        s = self.comfy_server()
        r = Runner({"comfyui": ComfyUI(s.url)}, self.state, self.out, sleep=nosleep, poll_seconds=0)
        a = r.create("comfyui", "workflow", {"workflow": self.wf()})
        r.run(a["id"])
        b = r.create("comfyui", "workflow", {"workflow": self.wf()})
        self.assertEqual(a["id"], b["id"])
        r.run(b["id"])
        self.assertEqual(len(s.posts("/prompt")), 1, "validated job must not be resubmitted")

    def test_metered_gpu_needs_budget(self):
        s = self.comfy_server()
        c = ComfyUI(s.url, gpu_rate_per_hour=1.8, rate_source="test")
        r = Runner({"comfyui": c}, self.state, self.out, sleep=nosleep, poll_seconds=0)
        job = r.run(r.create("comfyui", "workflow", {"workflow": self.wf(), "est_gpu_seconds": 60})["id"])
        self.assertEqual(job["state"], "blocked")
        self.assertEqual(s.posts("/prompt"), [])


# --------------------------------------------------------------------- budget
class BudgetTests(Base):
    def test_no_authorization_blocks(self):
        with self.assertRaisesRegex(BudgetError, "no spending authorization"):
            Budget(self.state).check("fal", Cost(0.1, "USD", "t"))

    def test_rules(self):
        self.authorize(expires=(dt.date.today() - dt.timedelta(days=1)).isoformat())
        with self.assertRaisesRegex(BudgetError, "expired"):
            Budget(self.state).check("fal", Cost(0.1, "USD", "t"))
        self.authorize()
        b = Budget(self.state)
        for cost, msg in ((None, "unknown"), (Cost(0.1, "EUR", "t"), "EUR"), (Cost(0.7, "USD", "t"), "per-job cap")):
            with self.assertRaisesRegex(BudgetError, msg):
                b.check("fal", cost)
        with self.assertRaisesRegex(BudgetError, "not in the authorized list"):
            b.check("openai", Cost(0.1, "USD", "t"))

    def test_total_cap_counts_open_reservations(self):
        self.authorize()
        b = Budget(self.state)
        b.reserve("j1#1", "fal", "m", Cost(0.5, "USD", "t"))
        b.reserve("j2#1", "fal", "m", Cost(0.4, "USD", "t"))
        with self.assertRaisesRegex(BudgetError, "total cap"):
            b.reserve("j3#1", "fal", "m", Cost(0.2, "USD", "t"))
        b.release("j2#1")
        b.settle("j1#1", 0.45)
        self.assertEqual(Budget(self.state).totals(), {"settled": 0.45, "reserved": 0.0, "committed": 0.45, "open": []})

    def test_prices_need_source_and_freshness(self):
        old = (dt.date.today() - dt.timedelta(days=45)).isoformat()
        t = PriceTable([{"provider": "fal", "model": "m", "unit": "per_video_second", "price": 0.1, "currency": "USD", "source": "u", "checked_on": TODAY},
                        {"provider": "fal", "model": "stale", "unit": "per_image", "price": 0.1, "currency": "USD", "source": "u", "checked_on": old},
                        {"provider": "fal", "model": "nosrc", "unit": "per_image", "price": 0.1, "currency": "USD", "checked_on": TODAY}])
        self.assertAlmostEqual(t.estimate("fal", "m", {"duration": 4}).amount, 0.4)
        self.assertIsNone(t.estimate("fal", "m", {}), "duration missing -> unknown")
        self.assertIsNone(t.estimate("fal", "stale", {}))
        self.assertIsNone(t.estimate("fal", "nosrc", {}))
        self.assertIsNone(t.estimate("fal", "absent", {}))


# --------------------------------------------------------------------- paid APIs
class PaidTests(Base):
    def prices(self, provider, model, unit="per_video_second", price=0.1):
        return PriceTable([{"provider": provider, "model": model, "unit": unit, "price": price,
                            "currency": "USD", "source": "https://example.test/pricing", "checked_on": TODAY}])

    def fal_server(self):
        n = {"status": 0}
        s = None

        def submit(req):
            return 200, {"request_id": "r1", "status_url": f"{s.url}/fal-ai/vid/requests/r1/status",
                         "response_url": f"{s.url}/fal-ai/vid/requests/r1"}

        def status(req):
            n["status"] += 1
            return 200, {"status": "IN_QUEUE" if n["status"] < 2 else "COMPLETED"}
        s = self.serve({
            ("POST", "/fal-ai/vid/i2v"): submit,
            ("GET", "/fal-ai/vid/requests/r1/status"): status,
            ("GET", "/fal-ai/vid/requests/r1"): lambda r: (200, {"video": {"url": f"{s.url}/files/out.mp4"}, "seed": 7}),
            ("GET", "/files/out.mp4"): lambda r: (200, self.mp4, "video/mp4"),
        })
        return s

    def test_fal_blocked_without_authorization_sends_nothing(self):
        s = self.fal_server()
        fal = FalQueue("SECRETKEY", self.prices("fal", "fal-ai/vid/i2v"), base_url=s.url)
        r = Runner({"fal": fal}, self.state, self.out, sleep=nosleep, poll_seconds=0)
        job = r.run(r.create("fal", "fal-ai/vid/i2v", {"prompt": "x", "duration": 4})["id"])
        self.assertEqual(job["state"], "blocked")
        self.assertIn("no spending authorization", job["blocked_reason"])
        self.assertEqual(s.posts(), [])

    def test_fal_unknown_price_blocked(self):
        self.authorize()
        s = self.fal_server()
        fal = FalQueue("K", PriceTable([]), base_url=s.url)
        r = Runner({"fal": fal}, self.state, self.out, sleep=nosleep, poll_seconds=0)
        job = r.run(r.create("fal", "fal-ai/vid/i2v", {"duration": 4})["id"])
        self.assertEqual((job["state"], s.posts()), ("blocked", []))

    def test_fal_authorized_runs_and_settles(self):
        self.authorize()
        s = self.fal_server()
        fal = FalQueue("SECRETKEY", self.prices("fal", "fal-ai/vid/i2v"), base_url=s.url)
        r = Runner({"fal": fal}, self.state, self.out, sleep=nosleep, poll_seconds=0)
        job = r.run(r.create("fal", "fal-ai/vid/i2v", {"prompt": "x", "duration": 4}, expect={"min_width": 64})["id"])
        self.assertEqual(job["state"], "validated", job.get("problems"))
        self.assertEqual(s.posts()[0]["headers"]["Authorization"], "Key SECRETKEY")
        t = Budget(self.state).totals()
        self.assertEqual((t["settled"], t["reserved"]), (0.4, 0.0))
        # cap: 1.0 total, 0.4 spent; a 0.6 job (per-job cap 0.6) fits once, then the next is refused
        j2 = r.run(r.create("fal", "fal-ai/vid/i2v", {"prompt": "y", "duration": 6})["id"])
        self.assertEqual(j2["state"], "validated")
        j3 = r.run(r.create("fal", "fal-ai/vid/i2v", {"prompt": "z", "duration": 1})["id"])
        self.assertEqual(j3["state"], "blocked")
        self.assertIn("total cap", j3["blocked_reason"])

    def test_unknown_submit_outcome_is_never_resubmitted(self):
        self.authorize()
        s = self.serve({("POST", "/fal-ai/vid/i2v"): lambda r: "drop"})
        fal = FalQueue("K", self.prices("fal", "fal-ai/vid/i2v"), base_url=s.url)
        r = Runner({"fal": fal}, self.state, self.out, sleep=nosleep, poll_seconds=0)
        jid = r.create("fal", "fal-ai/vid/i2v", {"duration": 4})["id"]
        self.assertEqual(r.run(jid)["state"], "submit_unknown")
        self.assertEqual(r.run(jid)["state"], "submit_unknown")
        self.assertEqual(len(s.posts()), 1)
        self.assertEqual(Budget(self.state).totals()["reserved"], 0.4, "reservation held until reconciled")

    def test_secret_is_redacted_from_errors(self):
        s = self.serve({("POST", "/x"): lambda r: (401, {"error": "bad key SECRETKEY"})})
        h = Http(s.url, headers={"Authorization": "Key SECRETKEY"}, secrets=("SECRETKEY",))
        with self.assertRaises(HttpError) as cm:
            h.post("/x", {})
        self.assertNotIn("SECRETKEY", str(cm.exception))

    def test_get_retries_transient_errors(self):
        n = {"i": 0}

        def flaky(req):
            n["i"] += 1
            return (503, {}) if n["i"] < 3 else (200, {"ok": True})
        s = self.serve({("GET", "/s"): flaky})
        self.assertEqual(Http(s.url, sleep=nosleep).get("/s"), {"ok": True})
        self.assertEqual(n["i"], 3)

    def test_replicate(self):
        self.authorize()
        s = None
        states = iter(["starting", "processing", "succeeded"])

        def pred(req):
            st = next(states)
            return 200, {"id": "pr1", "status": st, "output": [f"{s.url}/d/out.mp4"] if st == "succeeded" else None,
                         "metrics": {"predict_time": 12.5}}
        s = self.serve({("POST", "/models/acme/v2v/predictions"): lambda r: (201, {"id": "pr1", "status": "starting"}),
                        ("GET", "/predictions/pr1"): pred,
                        ("GET", "/d/out.mp4"): lambda r: (200, self.mp4, "video/mp4")})
        rep = Replicate("T", self.prices("replicate", "acme/v2v", "per_job", 0.3), base_url=s.url)
        r = Runner({"replicate": rep}, self.state, self.out, sleep=nosleep, poll_seconds=0)
        job = r.run(r.create("replicate", "acme/v2v", {"video": "x"})["id"])
        self.assertEqual(job["state"], "validated")
        self.assertEqual(json.loads(s.posts()[0]["body"]), {"input": {"video": "x"}})

    def test_runpod_base64_output_and_metered_cost(self):
        self.authorize()
        b64 = base64.b64encode(self.mp4).decode()
        s = self.serve({("POST", "/ep1/run"): lambda r: (200, {"id": "rp1", "status": "IN_QUEUE"}),
                        ("GET", "/ep1/status/rp1"): lambda r: (200, {"status": "COMPLETED", "executionTime": 90000,
                                                                     "output": {"images": [{"filename": "remi.mp4", "type": "base64", "data": b64}]}})})
        rp = RunPodComfy("ep1", "RPKEY", self.prices("runpod", "comfyui-worker", "per_gpu_second", 0.0005), base_url=s.url)
        r = Runner({"runpod": rp}, self.state, self.out, sleep=nosleep, poll_seconds=0)
        job = r.run(r.create("runpod", "comfyui-worker", {"workflow": {"1": {}}, "est_gpu_seconds": 120})["id"])
        self.assertEqual(job["state"], "validated", job.get("problems"))
        self.assertEqual(job["outputs"][0]["probe"]["frames"], 12)
        self.assertAlmostEqual(Budget(self.state).totals()["settled"], 0.045, msg="settled from reported executionTime (90 s)")


if __name__ == "__main__":
    unittest.main()
