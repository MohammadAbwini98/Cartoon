"""ComfyUI HTTP API backend (self-hosted on any cloud GPU host).

Endpoints used: GET /object_info (node discovery), POST /upload/image,
POST /prompt, GET /history/{id}, GET /queue, GET /view, POST /queue (delete).

request = {
  "workflow": {...},                 # API-format graph (Save → API format)
  "uploads": ["path/a.png", ...],    # sent with /upload/image first; reference them by file name
  "est_gpu_seconds": 120,            # for cost estimation on rented GPUs
}
"""
from __future__ import annotations

import json
import mimetypes
import os
import urllib.parse
import uuid

from remake_studio.providers.base import Capability, Cost, RemoteStatus
from remake_studio.providers.http import Http

# Node packs we rely on, keyed by a node class each one registers.
NODE_PACKS = {
    "core": "KSampler",
    "animatediff": "ADE_AnimateDiffLoaderGen1",
    "advanced_controlnet": "ACN_AdvancedControlNetApply",
    "controlnet_aux": "DWPreprocessor",
    "ipadapter": "IPAdapterAdvanced",
    "video_helper": "VHS_VideoCombine",
}


class ComfyUI:
    kind = "self_hosted"

    def __init__(self, base_url: str, token: str | None = None, name: str = "comfyui",
                 gpu_rate_per_hour: float | None = None, currency: str = "USD", rate_source: str = "",
                 http: Http | None = None):
        self.name = name
        self.billable = gpu_rate_per_hour is not None
        self.rate, self.currency, self.rate_source = gpu_rate_per_hour, currency, rate_source
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.http = http or Http(base_url, headers=headers, secrets=(token,) if token else ())
        self._object_info = None

    # -------------------------------------------------------------- discovery
    def object_info(self) -> dict:
        if self._object_info is None:
            self._object_info = self.http.get("/object_info")
        return self._object_info

    def node_packs(self) -> dict:
        info = self.object_info()
        return {pack: cls in info for pack, cls in NODE_PACKS.items()}

    def capabilities(self) -> list[Capability]:
        packs = self.node_packs()
        caps = [Capability("workflow", "workflow", tuple(p for p, ok in packs.items() if ok),
                           notes="any API-format graph whose nodes are installed")]
        if packs["core"]:
            caps.append(Capability("workflow", "image_to_image", ("image", "prompt")))
        if packs["animatediff"] and packs["advanced_controlnet"]:
            caps.append(Capability("workflow", "video_to_video", ("control_frames", "prompt", "reference_images")))
        return caps

    def missing_nodes(self, workflow: dict) -> list[str]:
        info = self.object_info()
        return sorted({n["class_type"] for n in workflow.values() if n.get("class_type") not in info})

    def models(self, folder: str) -> list[str]:
        """Model files the server lists for a loader input, e.g. folder='checkpoints'."""
        loaders = {"checkpoints": ("CheckpointLoaderSimple", "ckpt_name"),
                   "controlnet": ("ControlNetLoader", "control_net_name"),
                   "loras": ("LoraLoader", "lora_name"), "vae": ("VAELoader", "vae_name")}
        node, field = loaders[folder]
        spec = self.object_info().get(node, {}).get("input", {}).get("required", {}).get(field)
        return list(spec[0]) if spec and isinstance(spec[0], list) else []

    # -------------------------------------------------------------- cost
    def estimate(self, model: str, request: dict) -> Cost | None:
        if not self.billable:
            return Cost(0.0, self.currency, "self-hosted, no metered rate configured")
        secs = request.get("est_gpu_seconds")
        if secs is None:
            return None
        return Cost(round(self.rate / 3600 * secs, 6), self.currency,
                    f"{self.rate} {self.currency}/h × {secs} s ({self.rate_source})")

    # -------------------------------------------------------------- jobs
    def upload(self, path: str) -> str:
        boundary = uuid.uuid4().hex
        name = os.path.basename(path)
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            data = f.read()
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n"
                f"Content-Type: {ctype}\r\n\r\n").encode() + data + \
               f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n--{boundary}--\r\n".encode()
        r = self.http.post("/upload/image", raw_body=body,
                           headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        return r["name"]

    def submit(self, model: str, request: dict) -> str:
        wf = request["workflow"]
        missing = self.missing_nodes(wf)
        if missing:
            raise ValueError(f"server lacks node types: {missing}")
        for p in request.get("uploads", []):
            self.upload(p)
        r = self.http.post("/prompt", {"prompt": wf, "client_id": "remake-studio"})
        return r["prompt_id"]

    def status(self, remote_id: str) -> RemoteStatus:
        h = self.http.get(f"/history/{remote_id}")
        if remote_id in (h or {}):
            entry = h[remote_id]
            st = entry.get("status", {})
            outs = []
            for node_id, out in entry.get("outputs", {}).items():
                for key in ("images", "gifs", "videos"):
                    for item in out.get(key, []):
                        outs.append({"node": node_id, **item})
            if st.get("status_str") == "error":
                msgs = [m for m in st.get("messages", []) if m and m[0] == "execution_error"]
                return RemoteStatus("failed", error=json.dumps(msgs[-1][1])[:500] if msgs else "execution error", raw=entry)
            if st.get("completed", True):
                return RemoteStatus("succeeded", 1.0, outputs=outs, raw=entry)
        q = self.http.get("/queue")
        running = any(item[1] == remote_id for item in q.get("queue_running", []))
        pending = any(item[1] == remote_id for item in q.get("queue_pending", []))
        if running:
            return RemoteStatus("running")
        if pending:
            return RemoteStatus("queued")
        return RemoteStatus("failed", error="prompt not in queue or history (server restarted?)")

    def fetch(self, remote_id: str, status: RemoteStatus, dest_dir: str) -> list[str]:
        files = []
        for o in status.outputs:
            if o.get("type") == "temp":
                continue          # previews, not results
            q = urllib.parse.urlencode({"filename": o["filename"], "subfolder": o.get("subfolder", ""),
                                        "type": o.get("type", "output")})
            dest = os.path.join(dest_dir, f"{o['node']}_{os.path.basename(o['filename'])}")
            self.http.download(f"{self.http.base}/view?{q}", dest)
            files.append(dest)
        return files

    def cancel(self, remote_id: str) -> None:
        self.http.post("/queue", {"delete": [remote_id]})
