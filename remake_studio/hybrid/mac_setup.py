"""One-command, user-level setup of the Mac render worker (no sudo).

  sh infra/mac-worker/setup.sh --queue-endpoint https://<acct>.r2.cloudflarestorage.com --queue-bucket remake-queue

Steps (each recorded as pass / warn / fail in ~/.remake-worker/diagnostics.json):
  detect      macOS, Apple Silicon, memory, free disk, git
  comfyui     dedicated ComfyUI v0.43.2 at ~/RemakeStudio/ComfyUI with its own venv (default), or
              --reuse-comfyui PATH (your install is checked, never upgraded or downgraded)
  nodes       the five node packs at the pinned commits
  models      every pinned model by SHA-256; files already on disk (e.g. in another ComfyUI's models
              folder) are reused in place via extra_model_paths.yaml, never downloaded twice
  comfy_svc   user LaunchAgent running ComfyUI on 127.0.0.1 only, PYTORCH_ENABLE_MPS_FALLBACK=1
  loopback    ComfyUI answers on 127.0.0.1 and is NOT reachable on the LAN address
  mps         torch sees MPS, a matmul runs on it, ComfyUI reports device "mps", all pilot nodes load
  worker_cfg  ~/.remake-worker (mode 600): config, signing key, storage secrets (typed, never echoed)
  storage     queue bucket self-test incl. conditional writes; heartbeat written
  worker_svc  user LaunchAgent for the worker, benchmark-only (production stays disabled)
Nothing renders: the worker only accepts signed benchmark jobs until production is enabled.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMFY_TAG, COMFY_COMMIT = "v0.43.2", "423cbd20511becb1f16122ab518e92e58bfa03d9"
STANDARD_FOLDERS = ("checkpoints", "animatediff_models", "controlnet", "ipadapter", "clip_vision")


class System:
    """Real system access; tests substitute a fake."""

    def run(self, cmd, timeout=None, env=None, cwd=None) -> tuple[int, str]:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd)
            return r.returncode, (r.stdout + r.stderr).strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            return 127, str(e)

    def which(self, name):
        return shutil.which(name)

    def platform(self):
        return sys.platform, platform.machine()

    def http_get(self, url, timeout=5):
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())

    def can_connect(self, host, port, timeout=2.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def disk_free(self, path):
        return shutil.disk_usage(path).free

    def sleep(self, s):
        time.sleep(s)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


class Setup:
    def __init__(self, args, system: System | None = None, home: str | None = None, stdin_secret=getpass.getpass):
        self.a, self.sys = args, system or System()
        self.home = home or os.path.expanduser("~")
        self.cfg_dir = os.path.join(self.home, ".remake-worker")
        self.comfy = os.path.expanduser(args.reuse_comfyui) if args.reuse_comfyui else os.path.join(self.home, "RemakeStudio", "ComfyUI")
        self.reuse = bool(args.reuse_comfyui)
        self.py = os.path.join(self.comfy, ".venv", "bin", "python")
        self.annotators = os.path.join(self.comfy, "custom_nodes", "comfyui_controlnet_aux", "ckpts")
        self.results: list[dict] = []
        self.secret = stdin_secret
        self.data: dict = {}

    # -------------------------------------------------------------- helpers
    def rec(self, step, status, detail, **data):
        self.results.append({"step": step, "status": status, "detail": detail, **({"data": data} if data else {})})
        mark = {"pass": "✓", "warn": "!", "fail": "✗", "skip": "-"}[status]
        print(f"  {mark} {step:11s} {detail}")
        return status != "fail"

    def must(self, cmd, step, what, timeout=3600, cwd=None, env=None):
        rc, out = self.sys.run(cmd, timeout=timeout, cwd=cwd, env=env)
        if rc != 0:
            raise RuntimeError(f"{what} failed: {out[-600:]}")
        return out

    # -------------------------------------------------------------- steps
    def detect(self):
        osname, arch = self.sys.platform()
        if osname != "darwin":
            return self.rec("detect", "fail", f"this setup is for macOS (found {osname})")
        if arch != "arm64":
            return self.rec("detect", "fail", f"Apple Silicon required for MPS (found {arch})")
        _, chip = self.sys.run(["sysctl", "-n", "machdep.cpu.brand_string"])
        _, mem = self.sys.run(["sysctl", "-n", "hw.memsize"])
        _, ver = self.sys.run(["sw_vers", "-productVersion"])
        gb = int(mem or 0) / 2 ** 30
        free = self.sys.disk_free(self.home) / 2 ** 30
        self.data.update(chip=chip, memory_gb=round(gb, 1), macos=ver, disk_free_gb=round(free, 1))
        major = int((ver or "0").split(".")[0] or 0)
        if not self.sys.which("git"):
            return self.rec("detect", "fail", "git missing: run `xcode-select --install`, then rerun setup")
        if free < 40:
            return self.rec("detect", "fail", f"{free:.0f} GB free; need about 40 GB (ComfyUI env + 7.8 GB models + renders)")
        status = "pass" if major >= 14 else "warn"
        return self.rec("detect", status, f"{chip}, {gb:.0f} GB, macOS {ver}, {free:.0f} GB free"
                        + ("" if status == "pass" else " (macOS 14+ recommended for MPS)"))

    def python(self):
        rc, out = self.sys.run([self.a.python, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"])
        if rc != 0 or not re.fullmatch(r"3\.(1[0-3])", out.strip()):
            return self.rec("python", "fail", f"{self.a.python} is {out.strip() or 'unusable'}; need Python 3.10–3.13")
        self.data["python"] = out.strip()
        return self.rec("python", "pass", f"Python {out.strip()} ({self.a.python})")

    def comfyui(self):
        if self.reuse:
            if not os.path.exists(os.path.join(self.comfy, "main.py")):
                return self.rec("comfyui", "fail", f"{self.comfy} is not a ComfyUI folder")
            _, head = self.sys.run(["git", "-C", self.comfy, "rev-parse", "HEAD"])
            self.data["comfyui_commit"] = head
            if not os.path.exists(self.py):
                alt = os.path.join(self.comfy, "venv", "bin", "python")
                self.py = alt if os.path.exists(alt) else self.a.python
            same = head.strip() == COMFY_COMMIT
            return self.rec("comfyui", "pass" if same else "warn",
                            f"reusing {self.comfy}" + ("" if same else f" at {head[:7] or 'unknown'}; pinned {COMFY_TAG} (left unchanged)"))
        if not os.path.exists(os.path.join(self.comfy, "main.py")):
            os.makedirs(os.path.dirname(self.comfy), exist_ok=True)
            self.must(["git", "clone", "--depth", "1", "--branch", COMFY_TAG, "https://github.com/comfyanonymous/ComfyUI", self.comfy],
                      "comfyui", "cloning ComfyUI")
        _, head = self.sys.run(["git", "-C", self.comfy, "rev-parse", "HEAD"])
        if head.strip() != COMFY_COMMIT:
            return self.rec("comfyui", "fail", f"{self.comfy} is at {head[:7]}, expected {COMFY_TAG} ({COMFY_COMMIT[:7]})")
        marker = os.path.join(self.comfy, ".venv", ".remake-installed")
        if not os.path.exists(marker):
            self.must([self.a.python, "-m", "venv", os.path.join(self.comfy, ".venv")], "comfyui", "creating venv")
            self.must([self.py, "-m", "pip", "install", "--upgrade", "pip"], "comfyui", "upgrading pip")
            self.must([self.py, "-m", "pip", "install", "torch", "torchvision", "torchaudio"], "comfyui", "installing PyTorch")
            self.must([self.py, "-m", "pip", "install", "-r", os.path.join(self.comfy, "requirements.txt")], "comfyui",
                      "installing ComfyUI requirements")
            with open(marker, "w") as f:
                f.write(COMFY_COMMIT)
        return self.rec("comfyui", "pass", f"dedicated ComfyUI {COMFY_TAG} at {self.comfy}")

    def nodes(self):
        lock = json.load(open(os.path.join(REPO, "infra", "comfyui", "nodes.lock.json")))
        warns = []
        for n in lock["custom_nodes"]:
            d = os.path.join(self.comfy, "custom_nodes", n["name"])
            fresh = not os.path.exists(d)
            if fresh:
                os.makedirs(d)
                self.must(["git", "-C", d, "init", "-q"], "nodes", f"init {n['name']}")
            _, head = self.sys.run(["git", "-C", d, "rev-parse", "HEAD"])
            if head.strip() != n["commit"]:
                if self.reuse and not fresh:
                    warns.append(f"{n['name']} at {head[:7]} (pinned {n['commit'][:7]}; left unchanged)")
                    continue
                self.must(["git", "-C", d, "fetch", "-q", "--depth", "1", n["repo"], n["commit"]], "nodes", f"fetching {n['name']}")
                self.must(["git", "-C", d, "checkout", "-q", n["commit"]], "nodes", f"checking out {n['name']}")
            req = os.path.join(d, "requirements.txt")
            if os.path.exists(req) and (fresh or not self.reuse):
                self.must([self.py, "-m", "pip", "install", "-r", req], "nodes", f"requirements of {n['name']}")
        return self.rec("nodes", "warn" if warns else "pass",
                        "; ".join(warns) if warns else f"{len(lock['custom_nodes'])} node packs at pinned commits")

    def _search_dirs(self):
        dirs = [os.path.expanduser(p) for p in (self.a.models_from or [])]
        for c in ("ComfyUI", "Documents/ComfyUI", "comfy/ComfyUI", "Desktop/ComfyUI", "ComfyUI_windows_portable/ComfyUI"):
            m = os.path.join(self.home, c, "models")
            if os.path.isdir(m):
                dirs.append(m)
        own = os.path.join(self.comfy, "models")
        if os.path.isdir(own):
            dirs.append(own)
        return list(dict.fromkeys(os.path.abspath(d) for d in dirs))

    def models(self):
        sys.path.insert(0, os.path.join(REPO, "infra", "comfyui"))
        import fetch_models  # noqa: E402
        entries = json.load(open(os.path.join(REPO, "infra", "comfyui", "models.json")))["models"]
        shared, notes, downloaded, reused = set(), [], 0, 0
        search = self._search_dirs()
        for e in entries:
            if not e.get("sha256"):
                notes.append(f"{e['folder']}/{e['name']}: not chosen yet")
                continue
            root = self.annotators if e.get("store") == "annotators" else os.path.join(self.comfy, "models")
            target = os.path.join(root, e["folder"], e["name"])
            if os.path.exists(target) and sha256_file(target) == e["sha256"]:
                reused += 1
                continue
            found = next((os.path.join(d, e["folder"], e["name"]) for d in search
                          if os.path.exists(os.path.join(d, e["folder"], e["name"]))
                          and sha256_file(os.path.join(d, e["folder"], e["name"])) == e["sha256"]), None)
            if found and e.get("store") != "annotators":
                base = os.path.dirname(os.path.dirname(found))
                if base != os.path.abspath(os.path.join(self.comfy, "models")):
                    shared.add(base)
                reused += 1
                continue
            if found:          # annotators load from one folder: link the verified file there
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.symlink(found, target)
                reused += 1
                continue
            if self.a.dry_run:
                notes.append(f"would download {e['name']} ({e['bytes'] / 2 ** 30:.2f} GB)")
                continue
            fetch_models.fetch(e, root)
            downloaded += 1
        if shared and not self.reuse:
            with open(os.path.join(self.comfy, "extra_model_paths.yaml"), "w") as f:
                for i, base in enumerate(sorted(shared)):
                    f.write(f"remake_shared_{i}:\n    base_path: {base}\n" + "".join(f"    {k}: {k}\n" for k in STANDARD_FOLDERS))
        self.data["models"] = {"reused_in_place": reused, "downloaded": downloaded, "shared_dirs": sorted(shared)}
        detail = f"{reused} already present (verified), {downloaded} downloaded" + (f"; {'; '.join(notes)}" if notes else "")
        return self.rec("models", "warn" if notes else "pass", detail)

    def _plist(self, label, args, env):
        envxml = "".join(f"<key>{k}</key><string>{v}</string>" for k, v in env.items())
        argxml = "".join(f"<string>{x}</string>" for x in args)
        log = os.path.join(self.cfg_dir, f"{label.split('.')[-1]}.log")
        return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>'
                f"<key>Label</key><string>{label}</string><key>ProgramArguments</key><array>{argxml}</array>"
                f"<key>EnvironmentVariables</key><dict>{envxml}</dict><key>RunAtLoad</key><true/>"
                "<key>KeepAlive</key><true/><key>ProcessType</key><string>Background</string>"
                f"<key>StandardOutPath</key><string>{log}</string><key>StandardErrorPath</key><string>{log}</string>"
                "</dict></plist>\n")

    def _load_agent(self, label, plist):
        agents = os.path.join(self.home, "Library", "LaunchAgents")
        os.makedirs(agents, exist_ok=True)
        path = os.path.join(agents, f"{label}.plist")
        with open(path, "w") as f:
            f.write(plist)
        if not self.a.no_services:
            uid = str(os.getuid())
            self.sys.run(["launchctl", "bootout", f"gui/{uid}", path])
            self.must(["launchctl", "bootstrap", f"gui/{uid}", path], "agent", f"loading {label}")
        return path

    def comfy_service(self):
        if self.reuse:
            return self.rec("comfy_svc", "skip", f"using your own ComfyUI; start it with --listen 127.0.0.1 --port {self.a.port}")
        env = {"PYTORCH_ENABLE_MPS_FALLBACK": "1", "AUX_ANNOTATOR_CKPTS_PATH": self.annotators}
        p = self._load_agent("ai.remake-studio.comfyui",
                             self._plist("ai.remake-studio.comfyui", [self.py, os.path.join(self.comfy, "main.py"),
                                                                       "--listen", "127.0.0.1", "--port", str(self.a.port)], env))
        return self.rec("comfy_svc", "pass", f"LaunchAgent {p}")

    def loopback(self):
        url = f"http://127.0.0.1:{self.a.port}/system_stats"
        stats = None
        for _ in range(90):
            try:
                stats = self.sys.http_get(url)
                break
            except Exception:  # noqa: BLE001
                self.sys.sleep(2)
        if stats is None:
            return self.rec("loopback", "fail", f"ComfyUI did not answer on {url} within 3 minutes (see ~/.remake-worker/comfyui.log)")
        self.data["system_stats"] = stats
        _, lsof = self.sys.run(["lsof", "-nP", f"-iTCP:{self.a.port}", "-sTCP:LISTEN"])
        addrs = set(re.findall(r"TCP (\S+):%d \(LISTEN\)" % self.a.port, lsof))
        exposed = [a for a in addrs if a not in ("127.0.0.1", "[::1]", "localhost")]
        _, lan = self.sys.run(["ipconfig", "getifaddr", "en0"])
        lan_open = bool(lan.strip()) and self.sys.can_connect(lan.strip(), self.a.port)
        self.data["listen"] = sorted(addrs)
        if exposed or lan_open:
            return self.rec("loopback", "fail", f"ComfyUI is reachable beyond this Mac ({sorted(addrs)}, LAN {lan.strip()}); restart it with --listen 127.0.0.1")
        return self.rec("loopback", "pass", f"listening on {sorted(addrs) or ['127.0.0.1']} only; LAN address {lan.strip() or 'n/a'} refused")

    def mps(self):
        code = ("import torch,json;ok=torch.backends.mps.is_available();"
                "r=float((torch.ones(256,256,device='mps')@torch.ones(256,256,device='mps')).sum().cpu()) if ok else None;"
                "print(json.dumps({'torch':torch.__version__,'mps':ok,'matmul':r}))")
        rc, out = self.sys.run([self.py, "-c", code], timeout=120)
        try:
            t = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return self.rec("mps", "fail", f"PyTorch check failed: {out[-300:]}")
        dev = [d.get("type") for d in (self.data.get("system_stats") or {}).get("devices", [])]
        try:
            info = self.sys.http_get(f"http://127.0.0.1:{self.a.port}/object_info", timeout=30)
        except Exception:  # noqa: BLE001
            info = {}
        from remake_studio.hybrid import protocol as P
        need = set().union(*(t_["nodes"] for t_ in P.TEMPLATES.values()))
        missing = sorted(n for n in need if n not in info)
        self.data.update(torch=t, comfy_devices=dev, missing_nodes=missing)
        ok = t.get("mps") and t.get("matmul") == 256.0 * 256 * 256 and "mps" in dev and not missing
        return self.rec("mps", "pass" if ok else "fail",
                        f"torch {t.get('torch')}, MPS {'available' if t.get('mps') else 'NOT available'}, "
                        f"ComfyUI device {dev or '?'}" + (f", missing nodes {missing}" if missing else ", all pilot nodes loaded"))

    def worker_config(self):
        os.makedirs(os.path.join(self.cfg_dir, "state"), mode=0o700, exist_ok=True)
        os.chmod(self.cfg_dir, 0o700)
        key = os.path.join(self.cfg_dir, "job-signing.key")
        if not os.path.exists(key):
            with open(key, "w") as f:
                f.write(secrets.token_hex(32))
        os.chmod(key, 0o600)
        sec = os.path.join(self.cfg_dir, "secrets.json")
        cur = json.load(open(sec)) if os.path.exists(sec) else {}
        for k, prompt in (("REMAKE_STORE_ACCESS_KEY", "Queue bucket access key id (Mac token): "),
                          ("REMAKE_STORE_SECRET_KEY", "Queue bucket secret access key (Mac token, hidden): ")):
            if not cur.get(k):
                cur[k] = os.environ.get(k) or self.secret(prompt)
        fd = os.open(sec, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(cur, f)
        os.chmod(sec, 0o600)
        cfg = {"worker_id": self.a.worker_id, "comfyui_url": f"http://127.0.0.1:{self.a.port}",
               "comfyui_process_match": os.path.join(self.comfy, "main.py") if not self.reuse else "main.py",
               "transport": {"type": "s3", "endpoint": self.a.queue_endpoint, "bucket": self.a.queue_bucket, "region": "auto",
                             "access_key_env": "REMAKE_STORE_ACCESS_KEY", "secret_key_env": "REMAKE_STORE_SECRET_KEY"},
               "secrets_file": sec, "signing_key_file": key,
               "allowed_templates": ["smoke_v1", "keyframes_v1", "pilot_shot_v1"], "allowed_purposes": ["benchmark"],
               "input_url_hosts": [h for h in [re.sub(r"^https://([^/]+).*$", r"\1", self.a.queue_endpoint)] if h],
               "state_dir": os.path.join(self.cfg_dir, "state"), "poll_seconds": 15, "lease_seconds": 180}
        path = os.path.join(self.cfg_dir, "config.json")
        with open(path, "w") as f:
            json.dump(cfg, f, indent=2)
        os.chmod(path, 0o600)
        self.data["config"] = path
        return self.rec("worker_cfg", "pass", f"{self.cfg_dir} (mode 700; key, secrets and config mode 600); production disabled")

    def storage(self):
        sys.path.insert(0, REPO)
        from remake_studio.hybrid.worker import Worker
        env = dict(os.environ)
        env.update(json.load(open(os.path.join(self.cfg_dir, "secrets.json"))))
        try:
            w = Worker(json.load(open(os.path.join(self.cfg_dir, "config.json"))), env=env)
            problems = w.store.selftest()
            if not problems:
                w.heartbeat(note="setup")
        except SystemExit as e:
            return self.rec("storage", "fail", str(e))
        except Exception as e:  # noqa: BLE001
            return self.rec("storage", "fail", f"queue bucket not usable: {str(e)[:300]}")
        if problems:
            return self.rec("storage", "fail", "; ".join(problems))
        self.worker = w
        return self.rec("storage", "pass", f"{self.a.queue_bucket}: read/write/list/delete and conditional writes OK; heartbeat sent")

    def worker_service(self):
        p = self._load_agent("ai.remake-studio.worker",
                             self._plist("ai.remake-studio.worker",
                                         [self.py, "-m", "remake_studio.hybrid.worker", "--config", os.path.join(self.cfg_dir, "config.json")],
                                         {"PYTHONPATH": REPO}))
        return self.rec("worker_svc", "pass", f"LaunchAgent {p} (benchmark jobs only)")

    # -------------------------------------------------------------- run
    def run(self) -> dict:
        print("Remake Studio Mac worker setup")
        steps = [self.detect, self.python, self.comfyui, self.nodes, self.models, self.comfy_service,
                 self.loopback, self.mps, self.worker_config, self.storage, self.worker_service]
        for step in steps:
            try:
                ok = step()
            except RuntimeError as e:
                ok = self.rec(step.__name__, "fail", str(e))
            if not ok:
                break
        report = {"generated": time.time(), "worker_id": self.a.worker_id, "results": self.results,
                  "facts": {k: v for k, v in self.data.items() if k not in ("system_stats", "config")},
                  "ok": all(r["status"] != "fail" for r in self.results) and len(self.results) == len(steps)}
        os.makedirs(self.cfg_dir, exist_ok=True)
        with open(os.path.join(self.cfg_dir, "diagnostics.json"), "w") as f:
            json.dump(report, f, indent=2)
        if getattr(self, "worker", None):
            from remake_studio.hybrid import protocol as P
            self.worker.store.put(f"diagnostics/{self.a.worker_id}.json", P.canonical(report))
        print("setup complete: the worker is waiting for benchmark jobs" if report["ok"]
              else "setup stopped: see the failed step above (details in ~/.remake-worker/diagnostics.json)")
        return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--queue-endpoint", required=True)
    ap.add_argument("--queue-bucket", required=True)
    ap.add_argument("--worker-id", default="mac-m3pro")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--reuse-comfyui")
    ap.add_argument("--models-from", nargs="*")
    ap.add_argument("--port", type=int, default=8188)
    ap.add_argument("--no-services", action="store_true", help="write LaunchAgents but do not load them")
    ap.add_argument("--dry-run", action="store_true", help="do not download models")
    a = ap.parse_args(argv)
    return 0 if Setup(a).run()["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
