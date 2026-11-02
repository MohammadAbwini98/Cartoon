"""Paid API backends: RunPod serverless (ComfyUI worker), fal queue, Replicate.

Implemented from each service's documented request shapes; none has been
exercised against the live service from this project yet. Every call needs an
API key from the environment, every submit passes through the budget gate in
jobs.Runner first, and estimates come only from the sourced price table.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.parse

from remake_studio.providers.base import Capability, Cost, RemoteStatus
from remake_studio.providers.http import Http
from remake_studio.providers.pricing import PriceTable

MEDIA_EXT = (".mp4", ".webm", ".mov", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".wav", ".mp3")


def _media_urls(obj) -> list[str]:
    """Every URL in a JSON result that points at a media file."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "url" and isinstance(v, str) and v.startswith("http"):
                found.append(v)
            else:
                found += _media_urls(v)
    elif isinstance(obj, list):
        for v in obj:
            found += _media_urls(v)
    elif isinstance(obj, str) and obj.startswith("http") and urllib.parse.urlparse(obj).path.lower().endswith(MEDIA_EXT):
        found.append(obj)
    return found


def _save_urls(http: Http, urls: list[str], dest_dir: str) -> list[str]:
    out = []
    for i, u in enumerate(urls):
        name = os.path.basename(urllib.parse.urlparse(u).path) or f"output_{i}"
        out.append(http.download(u, os.path.join(dest_dir, f"{i:02d}_{name}")))
    return out


class RunPodComfy:
    """RunPod serverless endpoint running the ComfyUI worker image.
    input = {"workflow": {...}, "images": [{"name": "x.png", "image": "<base64>"}]}"""
    kind = "paid_api"
    name = "runpod"

    def __init__(self, endpoint_id: str, api_key: str, prices: PriceTable, gpu_model_key: str = "comfyui-worker",
                 base_url: str = "https://api.runpod.ai/v2", http: Http | None = None):
        self.model_key, self.prices = gpu_model_key, prices
        self.http = http or Http(f"{base_url}/{endpoint_id}", headers={"Authorization": f"Bearer {api_key}"},
                                 secrets=(api_key,))

    def capabilities(self):
        return [Capability(self.model_key, "workflow", ("workflow", "images"), notes="ComfyUI graph on a serverless GPU")]

    def estimate(self, model, request):
        return self.prices.estimate(self.name, model, request)

    def submit(self, model, request):
        images = []
        for p in request.get("uploads", []):
            with open(p, "rb") as f:
                images.append({"name": os.path.basename(p), "image": base64.b64encode(f.read()).decode()})
        r = self.http.post("/run", {"input": {"workflow": request["workflow"], "images": images}})
        return r["id"]

    def status(self, remote_id):
        r = self.http.get(f"/status/{remote_id}")
        st = {"IN_QUEUE": "queued", "IN_PROGRESS": "running", "COMPLETED": "succeeded", "FAILED": "failed",
              "CANCELLED": "canceled", "TIMED_OUT": "failed"}.get(r.get("status"), "running")
        secs = (r.get("executionTime") or 0) / 1000 or None
        raw = dict(r)
        if secs:
            c = self.prices.estimate(self.name, self.model_key, {"est_gpu_seconds": secs})
            if c:
                raw["cost"] = c.amount
        return RemoteStatus(st, error=r.get("error"), outputs=[r.get("output")], billed_seconds=secs, raw=raw)

    def fetch(self, remote_id, status, dest_dir):
        out = status.outputs[0] or {}
        files = []
        for i, im in enumerate(out.get("images", [])):
            name = os.path.basename(im.get("filename", f"out_{i}.png"))
            dest = os.path.join(dest_dir, f"{i:02d}_{name}")
            if im.get("type") == "base64":
                with open(dest, "wb") as f:
                    f.write(base64.b64decode(im["data"]))
                files.append(dest)
            elif im.get("type") in ("s3_url", "url"):
                files.append(self.http.download(im["data"], dest))
        return files

    def cancel(self, remote_id):
        self.http.post(f"/cancel/{remote_id}")


class FalQueue:
    """fal.ai queue API: submit → status_url → response_url."""
    kind = "paid_api"
    name = "fal"

    def __init__(self, api_key: str, prices: PriceTable, models: list[Capability] = (),
                 base_url: str = "https://queue.fal.run", http: Http | None = None):
        self.prices, self.models = prices, list(models)
        self.http = http or Http(base_url, headers={"Authorization": f"Key {api_key}"}, secrets=(api_key,))

    def capabilities(self):
        return self.models

    def estimate(self, model, request):
        return self.prices.estimate(self.name, model, request)

    def submit(self, model, request):
        r = self.http.post(f"/{model}", request)
        return json.dumps({"id": r["request_id"], "status_url": r["status_url"], "response_url": r["response_url"]})

    def status(self, remote_id):
        ref = json.loads(remote_id)
        r = self.http.get(ref["status_url"])
        st = {"IN_QUEUE": "queued", "IN_PROGRESS": "running", "COMPLETED": "succeeded"}.get(r.get("status"), "running")
        if st == "succeeded":
            res = self.http.get(ref["response_url"])
            if isinstance(res, dict) and res.get("detail") and not _media_urls(res):
                return RemoteStatus("failed", error=str(res.get("detail"))[:500], raw=res)
            return RemoteStatus("succeeded", 1.0, outputs=_media_urls(res), raw=res)
        return RemoteStatus(st, raw=r)

    def fetch(self, remote_id, status, dest_dir):
        return _save_urls(self.http, status.outputs, dest_dir)

    def cancel(self, remote_id):
        ref = json.loads(remote_id)
        self.http.post(ref["status_url"].rsplit("/status", 1)[0] + "/cancel")


class Replicate:
    """Replicate predictions API (official models: /models/{owner}/{name}/predictions)."""
    kind = "paid_api"
    name = "replicate"

    def __init__(self, api_token: str, prices: PriceTable, models: list[Capability] = (),
                 base_url: str = "https://api.replicate.com/v1", http: Http | None = None):
        self.prices, self.models = prices, list(models)
        self.http = http or Http(base_url, headers={"Authorization": f"Bearer {api_token}"}, secrets=(api_token,))

    def capabilities(self):
        return self.models

    def estimate(self, model, request):
        return self.prices.estimate(self.name, model, request)

    def submit(self, model, request):
        return self.http.post(f"/models/{model}/predictions", {"input": request})["id"]

    def status(self, remote_id):
        r = self.http.get(f"/predictions/{remote_id}")
        st = {"starting": "queued", "processing": "running", "succeeded": "succeeded",
              "failed": "failed", "canceled": "canceled"}.get(r.get("status"), "running")
        out = r.get("output")
        urls = _media_urls(out if isinstance(out, (list, dict)) else [out] if out else [])
        return RemoteStatus(st, error=r.get("error"), outputs=urls,
                            billed_seconds=(r.get("metrics") or {}).get("predict_time"), raw=r)

    def fetch(self, remote_id, status, dest_dir):
        return _save_urls(self.http, status.outputs, dest_dir)

    def cancel(self, remote_id):
        self.http.post(f"/predictions/{remote_id}/cancel")
