"""The Mac render worker as a Provider, so jobs.Runner, JobStore and the
artifact layout are the same for every backend.

request = {"graph": {...}, "template": "pilot_shot_v1", "variant": "mps",
           "inputs": ["/path/shot.mp4", "/path/ref.png"], "expect": {"frames": 72},
           "purpose": "production" | "benchmark", "expected_seconds": 600}
"""
from __future__ import annotations

import copy
import json
import os
import time

from remake_studio.hybrid import protocol as P
from remake_studio.providers.base import Capability, Cost, RemoteStatus


class MacWorkerQueue:
    kind = "self_hosted"
    billable = False

    def __init__(self, store, signing_key: bytes, worker_id: str = "mac-m3pro", name: str = "mac",
                 heartbeat_max_age: float = 120.0):
        self.store, self.key, self.worker_id, self.name = store, signing_key, worker_id, name
        self.max_age = heartbeat_max_age

    # ----------------------------------------------------------- availability
    def heartbeat(self) -> dict | None:
        b = self.store.get(P.k_worker(self.worker_id))
        return json.loads(b) if b else None

    def online(self, now=None) -> bool:
        hb = self.heartbeat()
        return bool(hb) and (now or time.time()) - hb["ts"] <= self.max_age and "error" not in (hb.get("comfyui") or {})

    def capabilities(self):
        hb = self.heartbeat() or {}
        return [Capability(t, "workflow", ("graph", "inputs"), notes=f"allowed on {self.worker_id}")
                for t in hb.get("allowed_templates", [])]

    def estimate(self, model, request):
        return Cost(0.0, "USD", "own hardware (electricity not metered)")

    # ----------------------------------------------------------- jobs
    def _put_input(self, path: str) -> dict:
        with open(path, "rb") as f:
            data = f.read()
        sha = P.sha256_bytes(data)
        if self.store.get(P.k_input(sha)) is None:
            self.store.put(P.k_input(sha), data)
        return {"name": os.path.basename(path), "key": P.k_input(sha), "sha256": sha, "bytes": len(data)}

    def submit(self, model, request):
        inputs = [self._put_input(p) for p in request.get("inputs", [])]
        graph = copy.deepcopy(request["graph"])
        ident = P.sha256_bytes(P.canonical({"t": request["template"], "v": request["variant"], "g": graph,
                                            "i": [i["sha256"] for i in inputs], "e": request.get("expect", {})}))
        jid = "m_" + ident[:24]
        for node in graph.values():
            if node.get("class_type") == "SaveImage":
                node["inputs"]["filename_prefix"] = f"remake/{jid}/frame"
        if self.store.get(P.k_job(jid)) is None:          # idempotent: same request, same job
            job = P.make_job(jid, request["template"], request["variant"], graph, inputs, request.get("expect"),
                             target=self.worker_id, purpose=request.get("purpose", "production"),
                             expected_seconds=request.get("expected_seconds"))
            self.store.put(P.k_job(jid), P.canonical(P.sign(job, self.key)), if_none_match=True)
        return jid

    def status(self, remote_id):
        b = self.store.get(P.k_status(remote_id))
        if not b:
            return RemoteStatus("queued", raw={"waiting_for": self.worker_id, "worker_online": self.online()})
        s = json.loads(b)
        state = {"claimed": "running", "preparing": "running", "running": "running", "uploading": "running",
                 "succeeded": "succeeded", "failed": "failed", "rejected": "failed", "canceled": "canceled"}[s["state"]]
        return RemoteStatus(state, s.get("progress"), s.get("error"), raw=s)

    def fetch(self, remote_id, status, dest_dir):
        m = json.loads(self.store.get(P.k_result(remote_id, "manifest.json")))
        out = []
        for f in m["files"]:
            data = self.store.get(P.k_result(remote_id, f["name"]))
            if data is None or P.sha256_bytes(data) != f["sha256"]:
                raise IOError(f"result {f['name']} missing or corrupted in transit")
            p = os.path.join(dest_dir, f["name"])
            with open(p, "wb") as fh:
                fh.write(data)
            out.append(p)
        return out

    def cancel(self, remote_id):
        self.store.put(P.k_cancel(remote_id), b"1")
