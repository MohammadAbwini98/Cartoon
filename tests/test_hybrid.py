"""Hybrid rendering: transport, signed jobs, the real worker process against a
ComfyUI stand-in process, cancellation, restart recovery, OOM reporting,
leases, Mac availability, and the scheduler's routing rules."""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from remake_studio.hybrid import protocol as P
from remake_studio.hybrid.mac_provider import MacWorkerQueue
from remake_studio.hybrid.scheduler import Scheduler, Task
from remake_studio.hybrid.transport import Conflict, LocalDirTransport, S3Transport, sigv4_authorization
from remake_studio.providers.base import Cost
from remake_studio.providers.budget import Budget
from remake_studio.providers.jobs import Runner
from remake_studio.workflows import PilotParams, pilot_shot_graph

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = b"k" * 48


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def wait_until(fn, timeout=30, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("condition not reached in time")


class Rig:
    """A shared store, a ComfyUI stand-in process and worker processes."""

    def __init__(self, d, **mock):
        self.d = d
        self.store_dir = os.path.join(d, "store")
        self.store = LocalDirTransport(self.store_dir)
        self.port = free_port()
        self.log = os.path.join(d, "comfy.log")
        self.state = os.path.join(d, "comfy_state.json")
        self.mock_opts = mock
        self.mock = None
        self.workers = []
        key = os.path.join(d, "job-signing.key")
        with open(key, "wb") as f:
            f.write(KEY)
        os.chmod(key, 0o600)
        self.cfg = os.path.join(d, "worker.json")
        cfg_f = open(self.cfg, "w")
        json.dump({"worker_id": "mac-m3pro", "transport": {"type": "local", "root": self.store_dir},
                   "signing_key_file": key, "comfyui_url": f"http://127.0.0.1:{self.port}",
                   "allowed_templates": ["smoke_v1", "keyframes_v1", "pilot_shot_v1"],
                   "state_dir": os.path.join(d, "worker_state"), "lease_seconds": 5, "poll_seconds": 0.3,
                   "job_poll_seconds": 0.3, "memory_sample_seconds": 0.3, "comfyui_process_match": "mock_comfyui"},
                  cfg_f)
        cfg_f.close()
        self.mac = MacWorkerQueue(self.store, KEY)

    def start_mock(self, keep_state=True, **override):
        opts = {**self.mock_opts, **override}
        cmd = [sys.executable, os.path.join(ROOT, "tests", "mock_comfyui.py"), "--port", str(self.port), "--log", self.log]
        if keep_state:
            cmd += ["--state", self.state]
        for k, v in opts.items():
            cmd += [f"--{k}", str(v)]
        self.mock = subprocess.Popen(cmd)
        wait_until(lambda: self._up())

    def _up(self):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/system_stats", timeout=1)
            return True
        except Exception:  # noqa: BLE001
            return False

    def stop_mock(self):
        if self.mock:
            self.mock.kill(); self.mock.wait(); self.mock = None

    def worker(self, once=True):
        env = {**os.environ, "PYTHONPATH": ROOT}
        p = subprocess.Popen([sys.executable, "-m", "remake_studio.hybrid.worker", "--config", self.cfg] + (["--once"] if once else []),
                             cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.workers.append(p)
        return p

    def events(self, kind):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [e for e in map(json.loads, f) if e["event"] == kind]

    def status(self, jid):
        b = self.store.get(P.k_status(jid))
        return json.loads(b) if b else None

    def submit(self, frames=6, template="pilot_shot_v1", purpose="benchmark", expected_seconds=None):
        shot = os.path.join(self.d, "shot.mp4"); ref = os.path.join(self.d, "ref.png")
        for p, b in ((shot, os.urandom(2000)), (ref, os.urandom(500))):
            if not os.path.exists(p):
                with open(p, "wb") as f:
                    f.write(b)
        g = pilot_shot_graph(PilotParams("shot.mp4", "ref.png", frames=frames, width=512, height=288))
        return self.mac.submit(template, {"graph": g, "template": template, "variant": "mps", "inputs": [shot, ref],
                                          "expect": {"frames": frames}, "purpose": purpose,
                                          "expected_seconds": expected_seconds})

    def close(self):
        for w in self.workers:
            if w.poll() is None:
                w.kill(); w.wait()
            for s in (w.stdout, w.stderr):
                if s:
                    s.close()
        self.stop_mock()


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.d = self._t.name
        self.rigs = []

    def tearDown(self):
        for r in self.rigs:
            r.close()
        self._t.cleanup()

    def rig(self, **mock):
        r = Rig(self.d, **mock)
        self.rigs.append(r)
        return r


# ------------------------------------------------------------------ transport
class TransportTests(Base):
    def test_sigv4_matches_aws_documentation_example(self):
        e = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        h = {"host": "examplebucket.s3.amazonaws.com", "range": "bytes=0-9", "x-amz-content-sha256": e,
             "x-amz-date": "20130524T000000Z"}
        a = sigv4_authorization("GET", "https://examplebucket.s3.amazonaws.com/test.txt", h, e, "AKIAIOSFODNN7EXAMPLE",
                                "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "us-east-1", "20130524T000000Z")
        self.assertTrue(a.endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"))

    def test_local_store_selftest_and_escape(self):
        t = LocalDirTransport(os.path.join(self.d, "s"))
        self.assertEqual(t.selftest(), [])
        with self.assertRaises(ValueError):
            t.put("../outside", b"x")

    def test_s3_transport_against_bucket_stand_in(self):
        objs, seen = {}, []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, body=b"", headers=None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

            def key(self):
                return self.path.split("?")[0].split("/", 2)[2] if self.path.count("/") >= 2 else ""

            def do_PUT(self):
                seen.append(self.headers.get("Authorization", ""))
                data = self.rfile.read(int(self.headers["Content-Length"]))
                k = self.key(); cur = objs.get(k)
                if self.headers.get("If-None-Match") == "*" and cur:
                    return self.reply(412)
                im = self.headers.get("If-Match")
                if im and (not cur or f'"{cur[1]}"' != im):
                    return self.reply(412)
                import hashlib
                objs[k] = (data, hashlib.md5(data).hexdigest())
                self.reply(200, headers={"ETag": f'"{objs[k][1]}"'})

            def do_GET(self):
                if "list-type=2" in self.path:
                    from urllib.parse import parse_qs, urlparse
                    pre = parse_qs(urlparse(self.path).query).get("prefix", [""])[0]
                    xml = "<ListBucketResult xmlns='http://s3.amazonaws.com/doc/2006-03-01/'><IsTruncated>false</IsTruncated>" + \
                        "".join(f"<Contents><Key>{k}</Key></Contents>" for k in objs if k.startswith(pre)) + "</ListBucketResult>"
                    return self.reply(200, xml.encode())
                cur = objs.get(self.key())
                if not cur:
                    return self.reply(404)
                self.reply(200, cur[0], {"ETag": f'"{cur[1]}"'})

            def do_DELETE(self):
                objs.pop(self.key(), None); self.reply(204)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            t = S3Transport(f"http://127.0.0.1:{srv.server_address[1]}", "remake", "AKTEST", "SECRET", "auto")
            self.assertEqual(t.selftest(), [])
            t.put("jobs/a.json", b"{}")
            with self.assertRaises(Conflict):
                t.put("jobs/a.json", b"{}", if_none_match=True)
            self.assertEqual(t.list("jobs/"), ["jobs/a.json"])
            self.assertTrue(all(s.startswith("AWS4-HMAC-SHA256 Credential=AKTEST/") and "/auto/s3/aws4_request" in s for s in seen))
        finally:
            srv.shutdown(); srv.server_close()


# ------------------------------------------------------------------ worker
class WorkerTests(Base):
    def test_benchmark_job_end_to_end_through_runner(self):
        r = self.rig(seconds=2, frames=6)
        r.start_mock()
        runner = Runner({"mac": r.mac}, os.path.join(self.d, "state"), os.path.join(self.d, "out"),
                        poll_seconds=0.3, timeout_seconds=1)
        g = pilot_shot_graph(PilotParams("shot.mp4", "ref.png", frames=6, width=512, height=288))
        for p in ("shot.mp4", "ref.png"):
            open(os.path.join(self.d, p), "wb").write(os.urandom(800))
        req = {"graph": g, "template": "pilot_shot_v1", "variant": "mps",
               "inputs": [os.path.join(self.d, "shot.mp4"), os.path.join(self.d, "ref.png")],
               "expect": {"frames": 6}, "purpose": "benchmark", "expected_seconds": 2}
        job = runner.create("mac", "pilot_shot_v1", req, req["inputs"], expect={"width": 64, "height": 36})
        job = runner.run(job["id"])
        self.assertIn(job["state"], ("submitted", "running"), "no worker yet: the job waits, state persisted")
        w = r.worker()
        self.assertEqual(w.wait(timeout=60), 0, w.stderr.read())
        runner.timeout = 10
        job = runner.run(job["id"])
        self.assertEqual(job["state"], "validated", job.get("problems"))
        self.assertEqual(len(job["outputs"]), 6)
        st = r.status(job["remote_id"])
        m = st["metrics"]
        self.assertEqual(m["output_frames"], 6)
        self.assertGreater(m["peak_comfyui_rss_mb"], 40, "ComfyUI process memory sampled")
        self.assertGreater(m["peak_system_used_mb"], 0)
        self.assertEqual(m["device"], "mps")
        self.assertGreater(m["sec_per_frame"], 0.25)
        self.assertGreater(m["effective_fps"], 0)
        hb = json.loads(r.store.get(P.k_worker("mac-m3pro")))
        self.assertEqual(hb["comfyui"]["version"], "0.43.2-mock")
        self.assertEqual(len(r.events("upload")), 2)
        from remake_studio.hybrid.dashboard import snapshot
        snap = snapshot(os.path.join(self.d, "state"), r.mac)
        self.assertTrue(snap["workers"][0]["online"])
        row = snap["jobs"][0]
        self.assertEqual((row["backend"], row["state"], row["worker_state"]), ("mac", "validated", "succeeded"))
        self.assertEqual(row["metrics"]["output_frames"], 6)

    def test_tampered_and_disallowed_jobs_are_rejected(self):
        r = self.rig(seconds=0.5)
        r.start_mock()
        jid = r.submit()
        job = json.loads(r.store.get(P.k_job(jid)))
        job["graph"]["10"]["inputs"]["text"] = "changed after signing"
        r.store.put(P.k_job(jid), P.canonical(job))
        self.assertEqual(r.worker().wait(timeout=30), 0)
        self.assertEqual(r.status(jid)["state"], "rejected")
        self.assertIn("signature", r.status(jid)["error"])
        # properly signed, but with a node outside the template
        bad = P.make_job("evil_job_1", "pilot_shot_v1", "mps",
                         {"1": {"class_type": "ExecutePythonCode", "inputs": {"code": "import os"}}}, [], target="mac-m3pro",
                         purpose="benchmark")
        r.store.put(P.k_job("evil_job_1"), P.canonical(P.sign(bad, KEY)))
        self.assertEqual(r.worker().wait(timeout=30), 0)
        self.assertEqual(r.status("evil_job_1")["state"], "rejected")
        self.assertIn("ExecutePythonCode", r.status("evil_job_1")["error"])
        self.assertEqual(r.events("prompt"), [], "nothing reached ComfyUI")

    def test_cancel_interrupts_running_prompt(self):
        r = self.rig(seconds=30)
        r.start_mock()
        jid = r.submit()
        w = r.worker()
        wait_until(lambda: (r.status(jid) or {}).get("phase") in ("rendering", "queued in ComfyUI"))
        r.mac.cancel(jid)
        self.assertEqual(w.wait(timeout=30), 0)
        self.assertEqual(r.status(jid)["state"], "canceled")
        self.assertEqual(len(r.events("interrupt")), 1)

    def test_worker_restart_recovers_without_rerendering(self):
        r = self.rig(seconds=4, frames=6)
        r.start_mock()
        jid = r.submit()
        w = r.worker()
        wait_until(lambda: (r.status(jid) or {}).get("prompt_id"))
        os.kill(w.pid, signal.SIGKILL); w.wait()
        self.assertEqual(r.status(jid)["state"], "running", "killed mid-job")
        w2 = r.worker()
        self.assertEqual(w2.wait(timeout=60), 0, w2.stderr.read())
        self.assertEqual(r.status(jid)["state"], "succeeded")
        self.assertEqual(len(r.events("prompt")), 1, "recovered the same prompt; no second render")

    def test_comfyui_restart_loses_prompt_and_worker_resubmits_once(self):
        r = self.rig(seconds=3, frames=6)
        r.start_mock(keep_state=False)
        jid = r.submit()
        w = r.worker()
        wait_until(lambda: (r.status(jid) or {}).get("prompt_id"))
        r.stop_mock()                      # ComfyUI dies mid-render
        wait_until(lambda: "not reachable" in (r.status(jid) or {}).get("phase", ""))
        r.start_mock(keep_state=False)     # comes back with an empty history
        self.assertEqual(w.wait(timeout=60), 0, w.stderr.read())
        self.assertEqual(r.status(jid)["state"], "succeeded")
        self.assertEqual(r.status(jid)["attempt"], 2)

    def test_out_of_memory_is_classified(self):
        r = self.rig(seconds=0.5, fail="MPS backend out of memory (MPS allocated: 17.10 GB)")
        r.start_mock()
        jid = r.submit()
        self.assertEqual(r.worker().wait(timeout=30), 0)
        st = r.status(jid)
        self.assertEqual((st["state"], st["error_kind"]), ("failed", "oom"))
        self.assertIn("17.10 GB", st["error"])

    def test_lease_expiry_allows_recovery_live_lease_does_not(self):
        r = self.rig(seconds=0.5)
        r.start_mock()
        jid = r.submit()
        r.store.put(P.k_claim(jid), P.canonical({"worker_id": "other-mac", "lease_until": time.time() + 300}))
        self.assertEqual(r.worker().wait(timeout=30), 0)
        self.assertIsNone(r.status(jid), "live lease held by another worker is respected")
        r.store.put(P.k_claim(jid), P.canonical({"worker_id": "other-mac", "lease_until": time.time() - 1}))
        self.assertEqual(r.worker().wait(timeout=30), 0)
        self.assertEqual(r.status(jid)["state"], "succeeded")

    def test_refuses_public_comfyui_and_loose_key_file(self):
        from remake_studio.hybrid.worker import Worker
        r = self.rig()
        cfg = json.load(open(r.cfg))
        with self.assertRaisesRegex(SystemExit, "loopback"):
            Worker({**cfg, "comfyui_url": "http://0.0.0.0:8188"})
        os.chmod(cfg["signing_key_file"], 0o644)
        with self.assertRaisesRegex(SystemExit, "chmod 600"):
            Worker(cfg)


# ------------------------------------------------------------------ benchmark CLI path
class BenchCliTests(Base):
    def test_cli_submits_smoke_stage_and_records_evidence(self):
        r = self.rig(seconds=0.5, frames=1)
        r.start_mock()
        state = os.path.join(self.d, "orch_state")
        toml = os.path.join(self.d, "hybrid.toml")
        with open(toml, "w") as f:
            f.write(f'worker_id = "mac-m3pro"\nsigning_key_env = "TEST_SIGNING_KEY"\n[transport]\ntype = "local"\nroot = "{r.store_dir}"\n')
        env = {**os.environ, "REMAKE_STATE": state, "REMAKE_HYBRID_CONFIG": toml, "TEST_SIGNING_KEY": KEY.decode(),
               "PYTHONPATH": ROOT}
        cli = lambda *a: subprocess.run([sys.executable, "-m", "remake_studio.cli", "hybrid", *a], cwd=self.d, env=env,
                                        capture_output=True, text=True, timeout=120)
        out = cli("bench", "submit")
        self.assertIn("queued B0", out.stdout, out.stderr)
        self.assertIn("already queued", cli("bench", "submit").stdout)
        self.assertEqual(r.worker().wait(timeout=60), 0)
        out = cli("bench", "collect")
        self.assertIn("B0: succeeded", out.stdout, out.stderr)
        compat = json.load(open(os.path.join(state, "compat.json")))
        self.assertEqual(compat["smoke_v1/mps"]["status"], "validated")
        out = cli("bench", "submit")
        self.assertIn("needs a source shot", out.stderr + out.stdout, "B1 requires the real source shot")


# ------------------------------------------------------------------ security (compromised queue)
class SecurityTests(Base):
    def signed(self, r, jid, mutate=None, **kw):
        g = pilot_shot_graph(PilotParams("shot.mp4", "ref.png", frames=6, width=512, height=288))
        for n in g.values():
            if n["class_type"] == "SaveImage":
                n["inputs"]["filename_prefix"] = f"remake/{jid}/frame"
        inputs = [r.mac._put_input(os.path.join(self.d, x)) for x in ("shot.mp4", "ref.png")]
        job = P.make_job(jid, "pilot_shot_v1", "mps", g, inputs, {"frames": 6}, purpose="benchmark", **kw)
        if mutate:
            mutate(job)
            job["graph_sha256"] = P.sha256_bytes(P.canonical(job["graph"]))
        r.store.put(P.k_job(jid), P.canonical(P.sign(job, KEY)))
        return job

    def setUp(self):
        super().setUp()
        for n in ("shot.mp4", "ref.png"):
            with open(os.path.join(self.d, n), "wb") as f:
                f.write(os.urandom(300))

    def run_worker(self, r):
        w = r.worker()
        out, err = w.communicate(timeout=60)
        return out + err

    def test_signed_but_malicious_jobs_never_reach_comfyui(self):
        r = self.rig(seconds=0.3)
        r.start_mock()
        cases = {
            "expired_job": dict(ttl_s=-10),
            "other_target": dict(target="someone-elses-mac"),
            "path_traversal": dict(mutate=lambda j: j["graph"]["1"]["inputs"].update(ckpt_name="../../.ssh/id_rsa")),
            "abs_path": dict(mutate=lambda j: j["graph"]["5"]["inputs"].update(image="/etc/passwd")),
            "extra_input": dict(mutate=lambda j: j["graph"]["2"]["inputs"].update(directory="/Users")),
            "huge_batch": dict(mutate=lambda j: j["graph"]["16"]["inputs"].update(batch_size=5000)),
            "huge_side": dict(mutate=lambda j: j["graph"]["16"]["inputs"].update(width=8192)),
            "bad_input_name": dict(mutate=lambda j: j["inputs"][0].update(name="../escape.mp4")),
        }
        for jid, kw in cases.items():
            self.signed(r, jid, **kw)
        for _ in cases:
            self.run_worker(r)
        for jid in cases:
            st = r.status(jid)
            if jid == "other_target":
                self.assertIsNone(st, "a job for another worker is not even picked up")
                continue
            self.assertEqual(st and st["state"], "rejected", f"{jid}: {st}")
        self.assertEqual(r.events("prompt"), [], "no malicious job reached ComfyUI")
        self.assertEqual(r.events("upload"), [], "no malicious job's files were uploaded")

    def test_replay_after_status_deleted_is_refused(self):
        r = self.rig(seconds=0.3)
        r.start_mock()
        self.signed(r, "replay_job")
        self.run_worker(r)
        self.assertEqual(r.status("replay_job")["state"], "succeeded")
        r.store.delete(P.k_status("replay_job"))           # attacker wipes the record and re-queues it
        self.run_worker(r)
        self.assertEqual(r.status("replay_job")["state"], "rejected")
        self.assertIn("replay", r.status("replay_job")["error"])
        self.assertEqual(len(r.events("prompt")), 1)

    def test_production_jobs_wait_until_enabled(self):
        r = self.rig(seconds=0.3)
        r.start_mock()
        self.signed(r, "prod_job", mutate=lambda j: j.update(purpose="production"))
        self.run_worker(r)
        self.assertIsNone(r.status("prod_job"), "left queued, not run and not rejected")

    def test_input_urls_limited_to_approved_https_hosts(self):
        r = self.rig(seconds=0.3)
        r.start_mock()
        def to_url(j):
            j["inputs"][0]["url"] = "http://evil.example/shot.mp4"
        self.signed(r, "url_job", mutate=to_url)
        self.run_worker(r)
        st = r.status("url_job")
        self.assertEqual((st["state"], st["error_kind"]), ("failed", "input"))
        self.assertEqual(r.events("prompt"), [])

    def test_forged_results_are_not_accepted(self):
        r = self.rig(seconds=0.3)
        r.start_mock()
        jid = r.submit()
        self.run_worker(r)
        m = json.loads(r.store.get(P.k_result(jid, "manifest.json")))
        self.assertTrue(P.verify_manifest(m, KEY))
        m["files"][0]["sha256"] = "0" * 64
        r.store.put(P.k_result(jid, "manifest.json"), P.canonical(m))
        with self.assertRaisesRegex(IOError, "signature"):
            r.mac.fetch(jid, None, self.d)

    def test_secrets_never_leave_the_machine(self):
        r = self.rig(seconds=0.3)
        r.start_mock()
        r.submit()
        logs = self.run_worker(r)
        self.assertNotIn(KEY.decode(), logs)
        for k in r.store.list(""):
            self.assertNotIn(KEY, r.store.get(k), f"signing key found in store object {k}")


# ------------------------------------------------------------------ scheduler
class FakeMac:
    name = "mac"
    def __init__(self, online): self._on = online
    def online(self): return self._on


class FakeCloud:
    name = "runpod"; kind = "paid_api"
    def __init__(self, amount): self.amount = amount
    def estimate(self, model, request): return Cost(self.amount, "USD", "test")


class SchedulerTests(Base):
    def sched(self, mac_online=True, cloud=True, authorize=True):
        state = tempfile.mkdtemp(dir=self.d)
        b = Budget(state)
        if authorize:
            os.makedirs(state, exist_ok=True)
            json.dump({"authorized_by": "test", "currency": "USD", "total_cap": 5, "per_job_cap": 2,
                       "providers": ["runpod"], "expires": "2999-01-01"}, open(os.path.join(state, "authorization.json"), "w"))
        return Scheduler(state, FakeMac(mac_online), {"runpod": FakeCloud(0.8)} if cloud else {}, budget=b)

    def validate(self, s, **kw):
        s.compat.record("pilot_shot_v1", "mps", status="validated", peak_memory_gb=11.0, sec_per_frame=9.0,
                        max_frames_tested=96, width=512, height=288, quality_equivalent=True, evidence=["m_x"], **kw)

    def t(self, **kw):
        return Task("comfy_workflow", "pilot_shot_v1", frames=72, width=512, height=288, **kw)

    def test_cpu_tasks(self):
        self.assertEqual(self.sched().decide(Task("encode")).backend, "cpu")

    def test_untested_mps_goes_to_cloud_never_assumed(self):
        d = self.sched().decide(self.t())
        self.assertEqual(d.backend, "cloud:runpod")
        self.assertIn("untested", d.rejected["mac"])

    def test_untested_and_no_budget_blocks(self):
        d = self.sched(authorize=False).decide(self.t())
        self.assertEqual((d.backend, d.action), (None, "blocked"))

    def test_benchmark_runs_only_on_mac_and_queues_when_offline(self):
        self.assertEqual(self.sched().decide(self.t(purpose="benchmark")).backend, "mac")
        d = self.sched(mac_online=False).decide(self.t(purpose="benchmark"))
        self.assertEqual((d.backend, d.action), ("mac", "queue_for_mac"))

    def test_validated_prefers_mac(self):
        s = self.sched(); self.validate(s)
        d = s.decide(self.t())
        self.assertEqual((d.backend, d.estimate["cost_usd"], d.estimate["seconds"]), ("mac", 0.0, 648.0))

    def test_offline_mac_waits_unless_fallback_allowed(self):
        s = self.sched(mac_online=False); self.validate(s)
        self.assertEqual(s.decide(self.t()).action, "queue_for_mac")
        d = s.decide(self.t(allow_fallback=True))
        self.assertEqual(d.backend, "cloud:runpod")
        s2 = self.sched(mac_online=False, authorize=False); self.validate(s2)
        self.assertEqual(s2.decide(self.t(allow_fallback=True)).action, "queue_for_mac")

    def test_limits_route_away_from_mac(self):
        s = self.sched(); self.validate(s)
        self.assertEqual(s.decide(Task("comfy_workflow", "pilot_shot_v1", frames=200, width=512, height=288)).backend, "cloud:runpod")
        self.assertEqual(s.decide(Task("comfy_workflow", "pilot_shot_v1", frames=48, width=1024, height=576)).backend, "cloud:runpod")
        s.compat.record("pilot_shot_v1", "mps", peak_memory_gb=16.5)
        self.assertIn("exceeds", s.decide(self.t()).rejected["mac"])

    def test_quality_must_be_confirmed(self):
        s = self.sched(); self.validate(s)
        s.compat.record("pilot_shot_v1", "mps", quality_equivalent=None)
        self.assertIn("quality", s.decide(self.t()).rejected["mac"])


if __name__ == "__main__":
    unittest.main()
