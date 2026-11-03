"""Render worker for a machine that already runs ComfyUI (built for the MacBook Pro M3 Pro / MPS).

  python3 -m remake_studio.hybrid.worker --config ~/.remake-worker/config.json [--once] [--selftest]

* Outbound only: polls the shared store (protocol.py); opens no port and offers no shell or desktop access.
* ComfyUI must be on loopback (127.0.0.1 / localhost / ::1); any other URL is refused at start-up.
* Runs a job only after verifying its HMAC signature, template allowlist, node allowlist and input hashes.
* Reports phase, estimated progress, memory (ComfyUI's /system_stats plus the ComfyUI process RSS) and errors.
* Cancellation: an orchestrator-written cancel marker interrupts the prompt.
* Restart recovery: the in-flight prompt id is kept in the state dir; after a restart the worker collects
  that prompt's result instead of rendering again, and resubmits only if ComfyUI lost it.
* Leases: a claim expires unless renewed, so a job held by a sleeping or crashed Mac can be recovered.
* No administrator rights: everything lives under the user's home; `caffeinate -i` (a user tool) keeps the
  Mac from idling to sleep only while a job runs.
Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

from remake_studio.hybrid import protocol as P
from remake_studio.hybrid.transport import Conflict, from_config
from remake_studio.providers.comfyui import ComfyUI
from remake_studio.providers.http import HttpError

OOM_PATTERNS = re.compile(r"out of memory|MPS backend out of memory|Invalid buffer size|OutOfMemory", re.I)


def _sh(*cmd) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def hardware() -> dict:
    if sys.platform == "darwin":
        return {"os": "macOS " + _sh("sw_vers", "-productVersion"), "chip": _sh("sysctl", "-n", "machdep.cpu.brand_string"),
                "memory_bytes": int(_sh("sysctl", "-n", "hw.memsize") or 0), "arch": platform.machine()}
    mem = 0
    try:
        mem = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError):
        pass
    return {"os": f"{platform.system()} {platform.release()}", "chip": platform.processor() or platform.machine(),
            "memory_bytes": mem, "arch": platform.machine()}


class MemorySampler(threading.Thread):
    """Samples ComfyUI's reported system memory use and the ComfyUI process RSS once per interval."""

    def __init__(self, comfy: ComfyUI, process_match: str, interval: float = 1.0):
        super().__init__(daemon=True)
        self.comfy, self.match, self.interval = comfy, process_match, interval
        self.stop_evt = threading.Event()
        self.peak_system_used = 0
        self.peak_rss = 0
        self.samples = 0
        self.device = None
        self.process_seen = False

    def _rss(self) -> int:
        out = _sh("ps", "-axo", "pid=,rss=,command=")
        total = 0
        for line in out.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) == 3 and self.match in parts[2] and "remake_studio.hybrid.worker" not in parts[2]:
                total += int(parts[1]) * 1024
        return total

    def sample(self):
        try:
            s = self.comfy.http.get("/system_stats")
            sysm = s.get("system", {})
            used = int(sysm.get("ram_total", 0)) - int(sysm.get("ram_free", 0))
            self.peak_system_used = max(self.peak_system_used, used)
            if s.get("devices"):
                self.device = s["devices"][0].get("type")
        except Exception:  # noqa: BLE001
            pass
        rss = self._rss()
        if rss:
            self.process_seen = True
        self.peak_rss = max(self.peak_rss, rss)
        self.samples += 1

    def run(self):
        while not self.stop_evt.is_set():
            self.sample()
            self.stop_evt.wait(self.interval)

    def stop(self) -> dict:
        self.stop_evt.set()
        self.join(timeout=5)
        return {"peak_system_used_mb": round(self.peak_system_used / 2 ** 20, 1),
                "peak_comfyui_rss_mb": round(self.peak_rss / 2 ** 20, 1), "memory_samples": self.samples,
                "device": self.device}


class Worker:
    def __init__(self, cfg: dict, transport=None, comfy: ComfyUI | None = None, env=os.environ):
        self.cfg = cfg
        self.id = cfg["worker_id"]
        url = cfg.get("comfyui_url", "http://127.0.0.1:8188")
        host = urllib.parse.urlparse(url).hostname
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise SystemExit(f"refusing ComfyUI at {url}: the worker only talks to a loopback ComfyUI")
        self.store = transport or from_config(cfg["transport"], env)
        self.comfy = comfy or ComfyUI(url)
        self.key = self._load_key(os.path.expanduser(cfg["signing_key_file"]))
        self.allowed = list(cfg.get("allowed_templates", []))
        self.state_dir = os.path.expanduser(cfg.get("state_dir", "~/.remake-worker/state"))
        os.makedirs(self.state_dir, exist_ok=True)
        self.inflight_path = os.path.join(self.state_dir, "inflight.json")
        self.lease = float(cfg.get("lease_seconds", 180))
        self.poll = float(cfg.get("poll_seconds", 10))
        self.job_poll = float(cfg.get("job_poll_seconds", 2))
        self.match = cfg.get("comfyui_process_match", "main.py")
        self.mem_interval = float(cfg.get("memory_sample_seconds", 1))
        self.busy = None
        # production renders stay off until the owner enables them in the config
        self.purposes = list(cfg.get("allowed_purposes", ["benchmark"]))
        self.url_hosts = list(cfg.get("input_url_hosts", []))
        self.ledger_path = os.path.join(self.state_dir, "executed.jsonl")
        self.schemas = P.load_schemas()

    # ------------------------------------------------------------ replay ledger
    def _ledger(self) -> dict:
        seen = {}
        if os.path.exists(self.ledger_path):
            with open(self.ledger_path) as f:
                for line in f:
                    e = json.loads(line)
                    seen[e["job_id"]] = e["nonce"]
        return seen

    def _ledger_add(self, job):
        with open(self.ledger_path, "a") as f:
            f.write(json.dumps({"job_id": job["job_id"], "nonce": job.get("nonce"), "t": time.time()}) + "\n")
            f.flush()
            os.fsync(f.fileno())

    @staticmethod
    def _load_key(path: str) -> bytes:
        st = os.stat(path)
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise SystemExit(f"{path} is readable by other users; run: chmod 600 {path}")
        with open(path, "rb") as f:
            key = f.read().strip()
        if len(key) < 32:
            raise SystemExit("signing key too short (need at least 32 bytes)")
        return key

    # ------------------------------------------------------------ store helpers
    def _json(self, key):
        b = self.store.get(key)
        return json.loads(b) if b else None

    def _status(self, job_id, state, **kw):
        prev = self._json(P.k_status(job_id)) or {}
        body = {**prev, "job_id": job_id, "worker": self.id, "state": state, "updated": time.time(), **kw}
        self.store.put(P.k_status(job_id), P.canonical(body))
        return body

    def heartbeat(self, note: str | None = None):
        info = {"worker_id": self.id, "ts": time.time(), "busy": self.busy, "accepting": True,
                "allowed_templates": self.allowed, "hardware": hardware(), "note": note}
        try:
            s = self.comfy.http.get("/system_stats")
            info["comfyui"] = {"version": s.get("system", {}).get("comfyui_version"),
                               "devices": [{"type": d.get("type"), "name": d.get("name")} for d in s.get("devices", [])],
                               "ram_total": s.get("system", {}).get("ram_total"), "ram_free": s.get("system", {}).get("ram_free")}
        except Exception as e:  # noqa: BLE001
            info["comfyui"] = {"error": f"ComfyUI not reachable: {e}"[:200]}
        self.store.put(P.k_worker(self.id), P.canonical(info))
        return info

    # ------------------------------------------------------------ claims
    def _claim(self, job_id) -> bool:
        body = P.canonical({"worker_id": self.id, "lease_until": time.time() + self.lease})
        try:
            self.store.put(P.k_claim(job_id), body, if_none_match=True)
            return True
        except Conflict:
            cur = self.store.get_etag(P.k_claim(job_id))
            if cur is None:
                return False
            c = json.loads(cur[0])
            if c["worker_id"] == self.id or c["lease_until"] < time.time():
                try:
                    self.store.put(P.k_claim(job_id), body, if_match=cur[1])
                    return True
                except Conflict:
                    return False
            return False

    def _renew(self, job_id) -> bool:
        cur = self.store.get_etag(P.k_claim(job_id))
        if cur is None or json.loads(cur[0])["worker_id"] != self.id:
            return False
        try:
            self.store.put(P.k_claim(job_id), P.canonical({"worker_id": self.id, "lease_until": time.time() + self.lease}),
                           if_match=cur[1])
            return True
        except Conflict:
            return False

    # ------------------------------------------------------------ selection
    def _pending(self) -> list[dict]:
        jobs = []
        for k in self.store.list("jobs/"):
            job = self._json(k)
            if not job or job.get("target") != self.id:
                continue
            if job.get("purpose") not in self.purposes:
                continue          # left queued: this worker does not run that purpose yet
            st = self._json(P.k_status(job["job_id"]))
            if st and st.get("state") in P.TERMINAL:
                continue
            if self.store.get(P.k_cancel(job["job_id"])) is not None:
                if not st or st.get("state") not in P.TERMINAL:
                    self._status(job["job_id"], "canceled", error="canceled before start")
                continue
            jobs.append(job)
        return sorted(jobs, key=lambda j: j.get("created", 0))

    # ------------------------------------------------------------ main loop
    def run_once(self) -> str | None:
        """Recover an in-flight job or take the oldest pending one. Returns the job id handled."""
        self.heartbeat()
        inflight = self._load_inflight()
        if inflight:
            job = self._json(P.k_job(inflight["job_id"]))
            if job:
                self._process(job, inflight)
                return job["job_id"]
            self._clear_inflight()
        for job in self._pending():
            if self._claim(job["job_id"]):
                self._process(job, None)
                return job["job_id"]
        return None

    def run_forever(self):
        while True:
            try:
                handled = self.run_once()
            except Exception as e:  # noqa: BLE001 — keep serving; report and back off
                print(f"worker error: {e}", file=sys.stderr)
                handled = None
            if not handled:
                time.sleep(self.poll)

    # ------------------------------------------------------------ inflight
    def _load_inflight(self):
        try:
            with open(self.inflight_path) as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def _save_inflight(self, d):
        tmp = self.inflight_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, self.inflight_path)

    def _clear_inflight(self):
        if os.path.exists(self.inflight_path):
            os.remove(self.inflight_path)

    # ------------------------------------------------------------ one job
    def _process(self, job, inflight):
        jid = job["job_id"]
        self.busy = jid
        caff = None
        try:
            if not P.verify(job, self.key):
                self._status(jid, "rejected", error="signature does not verify with this worker's key")
                return
            problems = P.validate(job, self.allowed, worker_id=self.id, schemas=self.schemas)
            if job.get("purpose") not in self.purposes:
                problems.append(f"purpose {job.get('purpose')!r} not enabled on this worker")
            seen = self._ledger()
            if inflight is None and jid in seen:
                problems.append("job already executed on this worker (replay)")
            if job.get("nonce") in seen.values() and seen.get(jid) != job.get("nonce"):
                problems.append("nonce already used by another job (replay)")
            if problems:
                self._status(jid, "rejected", error="; ".join(problems))
                return
            missing = self.comfy.missing_nodes(job["graph"])
            if missing:
                self._status(jid, "failed", error_kind="unsupported", error=f"nodes not installed here: {missing}")
                return
            if sys.platform == "darwin" and shutil.which("caffeinate"):
                caff = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            prompt_id = inflight.get("prompt_id") if inflight else None
            attempt = inflight.get("attempt", 1) if inflight else 1
            t_start = inflight.get("started", time.time()) if inflight else time.time()
            if prompt_id is None:
                self._ledger_add(job)
                prompt_id = self._submit(job)
                if prompt_id is None:
                    return
                self._save_inflight({"job_id": jid, "prompt_id": prompt_id, "attempt": attempt, "started": t_start})
            else:
                self._status(jid, "running", phase="recovered after worker restart", prompt_id=prompt_id)
            self._watch(job, prompt_id, attempt, t_start)
        finally:
            if caff:
                caff.terminate()
            self.busy = None

    def _submit(self, job):
        jid = job["job_id"]
        self._status(jid, "preparing", phase="downloading inputs", progress=0.02)
        tmp = tempfile.mkdtemp(prefix=f"remake-{jid}-")
        try:
            paths = []
            for i in job["inputs"]:
                try:
                    data = self._fetch_input(i)
                except Exception:  # noqa: BLE001 — network or store failure: fail this input, not the worker
                    data = None
                if data is None or P.sha256_bytes(data) != i["sha256"]:
                    self._status(jid, "failed", error_kind="input", error=f"input {i['name']} missing or hash mismatch")
                    return None
                p = os.path.join(tmp, i["name"])
                with open(p, "wb") as f:
                    f.write(data)
                paths.append(p)
            self._status(jid, "preparing", phase="uploading inputs to local ComfyUI", progress=0.05)
            for p in paths:
                self.comfy.upload(p)
            pid = self.comfy.http.post("/prompt", {"prompt": job["graph"], "client_id": f"remake-{self.id}"})["prompt_id"]
            self._status(jid, "running", phase="queued in ComfyUI", prompt_id=pid, progress=0.1)
            return pid
        except HttpError as e:
            self._status(jid, "failed", error_kind="comfyui_rejected", error=str(e)[:800])
            return None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _fetch_input(self, i) -> bytes | None:
        if i.get("url"):
            u = urllib.parse.urlparse(i["url"])
            if u.scheme != "https" or u.hostname not in self.url_hosts:
                return None
            with urllib.request.urlopen(i["url"], timeout=300) as r:
                return r.read()
        return self.store.get(i["key"])

    def _watch(self, job, prompt_id, attempt, t_start):
        jid = job["job_id"]
        sampler = MemorySampler(self.comfy, self.match, self.mem_interval)
        sampler.start()
        expected = job.get("expected_seconds")
        comfy_died = False
        try:
            while True:
                if self.store.get(P.k_cancel(jid)) is not None:
                    self._interrupt(prompt_id)
                    self._status(jid, "canceled", phase="canceled by orchestrator", metrics=sampler.stop())
                    self._clear_inflight()
                    return
                if not self._renew(jid):
                    self._interrupt(prompt_id)
                    self._status(jid, "failed", error_kind="lease_lost", error="claim taken over; stopped")
                    self._clear_inflight()
                    return
                try:
                    s = self.comfy.status(prompt_id)
                except HttpError as e:
                    if e.status != 0:
                        raise
                    comfy_died = comfy_died or sampler.process_seen
                    self._status(jid, "running", phase="ComfyUI not reachable; waiting for it to come back",
                                 prompt_id=prompt_id, comfyui_unreachable_since=time.time())
                    time.sleep(self.job_poll)
                    continue
                if s.state == "succeeded":
                    break
                if s.state == "failed":
                    if "not in queue or history" in (s.error or "") and attempt < 2:
                        # ComfyUI lost the prompt (it restarted); resubmit once
                        attempt += 1
                        new_id = self._submit(job)
                        if new_id is None:
                            return
                        prompt_id = new_id
                        self._save_inflight({"job_id": jid, "prompt_id": prompt_id, "attempt": attempt, "started": t_start})
                        continue
                    m = sampler.stop()
                    kind = "oom" if OOM_PATTERNS.search(s.error or "") else "execution"
                    if comfy_died:
                        kind = "comfyui_exited (possible out-of-memory kill)"
                    self._status(jid, "failed", error_kind=kind, error=(s.error or "")[:800], metrics=m, attempt=attempt)
                    self._clear_inflight()
                    return
                el = time.time() - t_start
                prog = 0.1 + 0.8 * min(0.99, el / expected) if expected else None
                self._status(jid, "running", phase="rendering" if s.state == "running" else "queued in ComfyUI",
                             prompt_id=prompt_id, progress=prog, progress_basis="elapsed vs expected" if expected else None,
                             elapsed_s=round(el, 1), memory_now={"peak_system_used_mb": round(sampler.peak_system_used / 2 ** 20, 1),
                                                               "peak_comfyui_rss_mb": round(sampler.peak_rss / 2 ** 20, 1)})
                time.sleep(self.job_poll)
            render_s = time.time() - t_start
            metrics = sampler.stop()
            self._status(jid, "uploading", phase="uploading results", progress=0.95)
            self._upload_results(job, prompt_id, s, render_s, metrics, attempt)
        finally:
            if sampler.is_alive():
                sampler.stop()

    def _interrupt(self, prompt_id):
        for call in (lambda: self.comfy.http.post("/interrupt", {}), lambda: self.comfy.cancel(prompt_id)):
            try:
                call()
            except Exception:  # noqa: BLE001
                pass

    def _upload_results(self, job, prompt_id, s, render_s, metrics, attempt):
        jid = job["job_id"]
        tmp = tempfile.mkdtemp(prefix=f"remake-out-{jid}-")
        try:
            files = self.comfy.fetch(prompt_id, s, tmp)
            manifest = []
            for f in sorted(files):
                with open(f, "rb") as fh:
                    data = fh.read()
                name = os.path.basename(f)
                self.store.put(P.k_result(jid, name), data)
                manifest.append({"name": name, "sha256": P.sha256_bytes(data), "bytes": len(data)})
            frames = sum(1 for m in manifest if m["name"].lower().endswith((".png", ".jpg", ".webp")))
            metrics = {**metrics, "render_wall_s": round(render_s, 2), "output_frames": frames,
                       "sec_per_frame": round(render_s / frames, 3) if frames else None,
                       "effective_fps": round(frames / render_s, 4) if frames and render_s else None}
            self.store.put(P.k_result(jid, "manifest.json"), P.canonical(P.sign_manifest(
                {"job_id": jid, "nonce": job.get("nonce"), "files": manifest, "prompt_id": prompt_id, "worker": self.id}, self.key)))
            exp = job.get("expect", {}).get("frames")
            if exp is not None and frames != exp:
                self._status(jid, "failed", error_kind="output", error=f"{frames} frames, expected {exp}", metrics=metrics)
            else:
                self._status(jid, "succeeded", progress=1.0, phase="done", metrics=metrics, attempt=attempt)
            self._clear_inflight()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="~/.remake-worker/config.json")
    ap.add_argument("--once", action="store_true", help="handle at most one job, then exit")
    ap.add_argument("--selftest", action="store_true", help="check the store (incl. conditional writes) and ComfyUI, then exit")
    a = ap.parse_args(argv)
    with open(os.path.expanduser(a.config)) as f:
        cfg = json.load(f)
    env = dict(os.environ)
    if cfg.get("secrets_file"):
        sp = os.path.expanduser(cfg["secrets_file"])
        if os.stat(sp).st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise SystemExit(f"{sp} is readable by other users; run: chmod 600 {sp}")
        with open(sp) as f:
            env.update(json.load(f))
    w = Worker(cfg, env=env)
    if a.selftest:
        problems = w.store.selftest()
        hb = w.heartbeat(note="selftest")
        print(json.dumps({"store_problems": problems, "comfyui": hb.get("comfyui"), "hardware": hb["hardware"]}, indent=2))
        return 1 if problems or "error" in hb.get("comfyui", {}) else 0
    if a.once:
        print(w.run_once() or "no job")
        return 0
    w.run_forever()


if __name__ == "__main__":
    sys.exit(main())
