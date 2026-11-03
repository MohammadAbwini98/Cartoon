"""Mac setup flow on a simulated Mac: real files, permissions, hashing, plists and the queue
bucket (S3 stand-in); macOS commands answered by a fake. Proves the orchestration, not the Mac."""
import argparse
import contextlib
import hashlib
import io
import json
import os
import stat
import tempfile
import unittest

from remake_studio.hybrid import mac_setup as M
from tests.s3_standin import S3Standin

PIN = M.COMFY_COMMIT


class FakeMac(M.System):
    def __init__(self, home, listen="127.0.0.1", mps=True, os_name="darwin", lan_open=False, node_heads=None):
        self.home, self.listen, self.mps, self.os, self.lan_open = home, listen, mps, os_name, lan_open
        self.cmds, self.heads = [], dict(node_heads or {})

    def platform(self):
        return self.os, "arm64"

    def which(self, n):
        return f"/usr/bin/{n}"

    def disk_free(self, p):
        return 200 * 2 ** 30

    def sleep(self, s):
        pass

    def can_connect(self, host, port, timeout=2.0):
        return self.lan_open

    def http_get(self, url, timeout=5):
        if url.endswith("/system_stats"):
            return {"system": {"comfyui_version": "0.43.2", "ram_total": 19327352832, "ram_free": 9e9},
                    "devices": [{"type": "mps" if self.mps else "cpu", "name": "mps"}]}
        from remake_studio.hybrid import protocol as P
        return {n: {} for t in P.TEMPLATES.values() for n in t["nodes"]}

    def run(self, cmd, timeout=None, env=None, cwd=None):
        self.cmds.append(cmd)
        c = " ".join(cmd)
        if cmd[:2] == ["sysctl", "-n"]:
            return 0, "Apple M3 Pro" if "brand" in cmd[2] else str(18 * 2 ** 30)
        if cmd[0] == "sw_vers":
            return 0, "15.1"
        if cmd[:2] == ["git", "clone"]:
            os.makedirs(cmd[-1], exist_ok=True)
            open(os.path.join(cmd[-1], "main.py"), "w").close()
            open(os.path.join(cmd[-1], "requirements.txt"), "w").close()
            self.heads[cmd[-1]] = PIN
            return 0, ""
        if cmd[0] == "git" and "rev-parse" in cmd:
            return 0, self.heads.get(cmd[2], "")
        if cmd[0] == "git" and "checkout" in cmd:
            self.heads[cmd[2]] = cmd[-1]
            return 0, ""
        if cmd[0] == "git":
            return 0, ""
        if "-c" in cmd and "version_info" in c:
            return 0, "3.12"
        if "-c" in cmd and "mps" in c:
            return 0, json.dumps({"torch": "2.9.0", "mps": self.mps, "matmul": 16777216.0 if self.mps else None})
        if "-m" in cmd and "venv" in cmd:
            os.makedirs(os.path.join(cmd[-1], "bin"), exist_ok=True)
            open(os.path.join(cmd[-1], "bin", "python"), "w").close()
            return 0, ""
        if cmd[0] == "lsof":
            return 0, f"Python 4242 me 9u IPv4 0x1 0t0 TCP {self.listen}:8188 (LISTEN)"
        if cmd[:2] == ["ipconfig", "getifaddr"]:
            return 0, "192.168.1.20"
        return 0, ""          # pip, launchctl


class MacSetup(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.home = self._t.name
        self.s3 = S3Standin(access_key="AKMAC", secret_key="mac-queue-secret", bucket="remake-queue")
        os.environ["REMAKE_STORE_ACCESS_KEY"], os.environ["REMAKE_STORE_SECRET_KEY"] = "AKMAC", "mac-queue-secret"
        # two small "models" already present in another ComfyUI, matching a test manifest
        self.other = os.path.join(self.home, "ComfyUI", "models")
        self.entries = []
        for store, folder, name in ((None, "controlnet", "cn.pth"), ("annotators", "yzd-v/DWPose", "yolox_l.onnx")):
            p = os.path.join(self.other, folder, name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            data = os.urandom(1000)
            open(p, "wb").write(data)
            self.entries.append({"store": store, "folder": folder, "name": name, "url": "https://huggingface.co/x/resolve/" + "0" * 40 + "/f",
                                 "sha256": hashlib.sha256(data).hexdigest(), "bytes": 1000})
        self.entries.append({"folder": "checkpoints", "name": "CHECKPOINT_TBD.safetensors", "url": None, "sha256": None})

    def tearDown(self):
        self.s3.close()
        for k in ("REMAKE_STORE_ACCESS_KEY", "REMAKE_STORE_SECRET_KEY"):
            os.environ.pop(k, None)
        self._t.cleanup()

    def setup(self, fake=None, **kw):
        a = argparse.Namespace(queue_endpoint=self.s3.endpoint, queue_bucket="remake-queue", worker_id="mac-m3pro",
                               python="/usr/bin/python3", reuse_comfyui=None, models_from=None, port=8188,
                               no_services=True, dry_run=False)
        for k, v in kw.items():
            setattr(a, k, v)
        fake = fake or FakeMac(self.home)
        s = M.Setup(a, fake, home=self.home, stdin_secret=lambda p: self.fail("must not prompt: secrets come from env"))
        manifest = os.path.join(self.home, "models.json")
        json.dump({"models": self.entries}, open(manifest, "w"))
        import remake_studio.hybrid.mac_setup as mod
        real = mod.REPO
        orig_join = os.path.join

        def models(self_=s):
            # point the models step at the test manifest
            M_orig = mod.Setup.models
            return M_orig(self_)
        s._manifest = manifest
        out = io.StringIO()
        old = mod.REPO
        try:
            with contextlib.redirect_stdout(out):
                with _patched_manifest(manifest):
                    r = s.run()
        finally:
            mod.REPO = old
        return r, fake, out.getvalue()

    def test_fresh_install_end_to_end(self):
        r, fake, out = self.setup()
        steps = {x["step"]: x["status"] for x in r["results"]}
        self.assertTrue(r["ok"], r["results"])
        self.assertEqual(steps["models"], "warn", "checkpoint not chosen yet is reported, not hidden")
        cmds = [" ".join(c) for c in fake.cmds]
        self.assertTrue(any("clone --depth 1 --branch v0.43.2" in c for c in cmds))
        self.assertTrue(any("pip install torch torchvision torchaudio" in c for c in cmds))
        self.assertEqual(sum("checkout -q" in c for c in cmds), 5, "five pinned node packs")
        self.assertFalse(any(c.startswith("launchctl bootstrap") for c in cmds), "--no-services")
        self.assertFalse(any("sudo" in c for c in cmds))
        cfgdir = os.path.join(self.home, ".remake-worker")
        for f in ("job-signing.key", "secrets.json", "config.json"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(cfgdir, f)).st_mode), 0o600, f)
        cfg = json.load(open(os.path.join(cfgdir, "config.json")))
        self.assertEqual(cfg["allowed_purposes"], ["benchmark"], "production disabled after setup")
        plist = open(os.path.join(self.home, "Library/LaunchAgents/ai.remake-studio.comfyui.plist")).read()
        self.assertIn("<string>--listen</string><string>127.0.0.1</string>", plist)
        self.assertIn("PYTORCH_ENABLE_MPS_FALLBACK", plist)
        # models reused in place, nothing downloaded; annotator linked; shared dir registered
        self.assertEqual(r["facts"]["models"]["downloaded"], 0)
        self.assertTrue(os.path.islink(os.path.join(self.home, "RemakeStudio/ComfyUI/custom_nodes/comfyui_controlnet_aux/ckpts/yzd-v/DWPose/yolox_l.onnx")))
        self.assertIn(f"base_path: {self.other}", open(os.path.join(self.home, "RemakeStudio/ComfyUI/extra_model_paths.yaml")).read())
        # the bucket received a heartbeat and the diagnostics, and no secret anywhere
        self.assertIn("workers/mac-m3pro.json", self.s3.objects)
        diag = self.s3.objects["diagnostics/mac-m3pro.json"][0]
        for secret in (b"mac-queue-secret", open(os.path.join(cfgdir, "job-signing.key"), "rb").read()):
            self.assertNotIn(secret, diag)
            self.assertNotIn(secret.decode(), out)
        self.assertEqual(self.s3.denied, [])

    def test_not_a_mac_stops_immediately(self):
        r, fake, _ = self.setup(FakeMac(self.home, os_name="linux"))
        self.assertFalse(r["ok"])
        self.assertEqual([x["step"] for x in r["results"]], ["detect"])

    def test_comfyui_exposed_on_lan_fails(self):
        r, first, _ = self.setup(FakeMac(self.home, listen="*"))
        self.assertEqual(r["results"][-1]["step"], "loopback")
        self.assertEqual(r["results"][-1]["status"], "fail")
        r, _, _ = self.setup(FakeMac(self.home, lan_open=True, node_heads=first.heads))
        self.assertEqual((r["results"][-1]["step"], r["results"][-1]["status"]), ("loopback", "fail"))

    def test_no_mps_fails(self):
        r, _, _ = self.setup(FakeMac(self.home, mps=False))
        self.assertEqual((r["results"][-1]["step"], r["results"][-1]["status"]), ("mps", "fail"))

    def test_reuse_existing_comfyui_never_changes_it(self):
        mine = os.path.join(self.home, "ComfyUI")
        open(os.path.join(mine, "main.py"), "w").close()
        nodes = os.path.join(mine, "custom_nodes", "ComfyUI-AnimateDiff-Evolved")
        os.makedirs(nodes)
        fake = FakeMac(self.home, node_heads={mine: "a" * 40, nodes: "b" * 40})
        r, fake, _ = self.setup(fake, reuse_comfyui=mine)
        steps = {x["step"]: x for x in r["results"]}
        self.assertEqual(steps["comfyui"]["status"], "warn")
        self.assertIn("left unchanged", steps["nodes"]["detail"])
        cmds = [" ".join(c) for c in fake.cmds]
        self.assertFalse(any(f"-C {nodes} checkout" in c for c in cmds), "existing node pack not touched")
        self.assertFalse(any("clone" in c for c in cmds))
        self.assertEqual(steps["comfy_svc"]["status"], "skip")


@contextlib.contextmanager
def _patched_manifest(path):
    real_open = open
    target = os.path.join(M.REPO, "infra", "comfyui", "models.json")

    def fake_open(p, *a, **k):
        return real_open(path if os.path.abspath(str(p)) == target else p, *a, **k)
    import builtins
    builtins.open = fake_open
    try:
        yield
    finally:
        builtins.open = real_open


if __name__ == "__main__":
    unittest.main()
