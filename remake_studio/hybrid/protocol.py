"""Job protocol between the cloud orchestrator and render workers.

Standard library only: the Mac worker imports this module without extra
packages. Layout in the shared store (all keys relative to the bucket/root):

  jobs/<job_id>.json            signed job envelope (written by the orchestrator)
  claims/<job_id>.json          {worker_id, lease_until}      (worker; conditional writes)
  status/<job_id>.json          state, progress, metrics, error (worker)
  cancel/<job_id>               presence = cancel requested    (orchestrator)
  inputs/<sha256>               input files, content-addressed (orchestrator)
  results/<job_id>/<file>       outputs, plus results/<job_id>/manifest.json (worker)
  workers/<worker_id>.json      heartbeat: time, hardware, ComfyUI, busy job (worker)

Security model: the worker executes nothing but ComfyUI graphs on its own
loopback ComfyUI. A job runs only if its HMAC signature verifies with the key
installed at authorization time, its template is on the worker's allowlist,
every node in its graph belongs to that template, every file it loads is one of
its own verified inputs, and its outputs go under remake/<job_id>/.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time

PROTOCOL = "remake-job/1"

# Node classes each template may contain. A graph with any other node is refused.
_PILOT_NODES = {"CheckpointLoaderSimple", "CLIPTextEncode", "EmptyLatentImage", "KSampler", "VAEDecode",
                "LoadImage", "SaveImage", "VHS_LoadVideo", "AnimeLineArtPreprocessor", "DWPreprocessor",
                "IPAdapterUnifiedLoader", "IPAdapterAdvanced", "ACN_ControlNetLoaderAdvanced",
                "ACN_AdvancedControlNetApply_v2"}
TEMPLATES = {
    "smoke_v1": {"nodes": {"LoadImage", "ImageScale", "SaveImage"}, "variants": ["mps", "cuda", "cpu"],
                 "purpose": "first contact: node loading, upload, output retrieval"},
    "keyframes_v1": {"nodes": _PILOT_NODES, "variants": ["mps", "cuda"],
                     "purpose": "per-frame redesign (no motion module): the six-frame test"},
    "pilot_shot_v1": {"nodes": _PILOT_NODES | {"ADE_StandardStaticContextOptions", "ADE_AnimateDiffLoaderGen1"},
                      "variants": ["mps", "cuda"], "purpose": "temporally consistent shot animation"},
}
FILE_INPUTS = {("LoadImage", "image"), ("VHS_LoadVideo", "video")}
TERMINAL = ("succeeded", "failed", "canceled", "rejected")


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sign(job: dict, key: bytes) -> dict:
    body = {k: v for k, v in job.items() if k != "sig"}
    return {**body, "sig": hmac.new(key, canonical(body), hashlib.sha256).hexdigest()}


def verify(job: dict, key: bytes) -> bool:
    sig = job.get("sig")
    if not isinstance(sig, str):
        return False
    body = {k: v for k, v in job.items() if k != "sig"}
    return hmac.compare_digest(sig, hmac.new(key, canonical(body), hashlib.sha256).hexdigest())


JOB_TTL_S = 7 * 86400
CLOCK_SKEW_S = 300
# Hard caps on what one job may ask of the machine (a signed job still cannot exhaust it).
LIMITS = {"max_frames": 192, "max_side": 1536, "max_steps": 60}
SCHEMAS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                            "workflows", "node_schemas.json")


def load_schemas(path: str = SCHEMAS_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


def make_job(job_id: str, template: str, variant: str, graph: dict, inputs: list[dict], expect: dict | None = None,
             target: str = "mac-m3pro", purpose: str = "production", expected_seconds: float | None = None,
             ttl_s: float = JOB_TTL_S) -> dict:
    """inputs: [{"name": "shot.mp4", "key": "inputs/<sha>", "sha256": "<sha>", "bytes": n}]
    `target` names exactly one worker; `nonce` + `expires` make a signed job single-use and short-lived."""
    now = time.time()
    return {"protocol": PROTOCOL, "job_id": job_id, "template": template, "variant": variant, "target": target,
            "purpose": purpose, "graph": graph, "graph_sha256": sha256_bytes(canonical(graph)), "inputs": inputs,
            "expect": expect or {}, "expected_seconds": expected_seconds, "created": now, "expires": now + ttl_s,
            "nonce": secrets.token_hex(16)}


def sign_manifest(manifest: dict, key: bytes) -> dict:
    body = {k: v for k, v in manifest.items() if k != "sig"}
    return {**body, "sig": hmac.new(key, b"result:" + canonical(body), hashlib.sha256).hexdigest()}


def verify_manifest(manifest: dict, key: bytes) -> bool:
    body = {k: v for k, v in manifest.items() if k != "sig"}
    want = hmac.new(key, b"result:" + canonical(body), hashlib.sha256).hexdigest()
    return isinstance(manifest.get("sig"), str) and hmac.compare_digest(manifest["sig"], want)


def validate(job: dict, allowed_templates: list[str], worker_id: str | None = None, now: float | None = None,
             schemas: dict | None = None) -> list[str]:
    """Everything that must hold before a worker touches the job (signature checked separately)."""
    p = []
    now = time.time() if now is None else now
    if worker_id is not None and job.get("target") != worker_id:
        p.append(f"job is addressed to {job.get('target')!r}, not this worker")
    exp, created = job.get("expires"), job.get("created")
    if not isinstance(exp, (int, float)) or not isinstance(created, (int, float)):
        p.append("job has no creation/expiry time")
    else:
        if exp < now:
            p.append("job expired")
        if created > now + CLOCK_SKEW_S or exp - created > JOB_TTL_S + 1:
            p.append("job times are out of range")
    if not re.fullmatch(r"[0-9a-f]{32}", str(job.get("nonce", ""))):
        p.append("job has no valid nonce")
    schemas = schemas if schemas is not None else load_schemas()
    if job.get("protocol") != PROTOCOL:
        p.append(f"unsupported protocol {job.get('protocol')!r}")
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,80}", str(job.get("job_id", ""))):
        p.append("bad job id")
    t = job.get("template")
    if t not in TEMPLATES:
        return p + [f"unknown template {t!r}"]
    if t not in allowed_templates:
        p.append(f"template {t!r} not allowed on this worker")
    if job.get("variant") not in TEMPLATES[t]["variants"]:
        p.append(f"variant {job.get('variant')!r} not defined for {t}")
    graph = job.get("graph") or {}
    if job.get("graph_sha256") != sha256_bytes(canonical(graph)):
        p.append("graph hash mismatch")
    names = {i["name"] for i in job.get("inputs", [])}
    for nid, node in graph.items():
        ct = node.get("class_type")
        if ct not in TEMPLATES[t]["nodes"]:
            p.append(f"node {nid}: {ct!r} not permitted in {t}")
        sc = schemas.get(ct)
        if ct in TEMPLATES[t]["nodes"] and sc is None:
            p.append(f"node {nid}: no input schema for {ct}")
        for k, v in (node.get("inputs") or {}).items():
            if sc is not None and k not in sc["required"] and k not in sc["optional"]:
                p.append(f"node {nid}: {ct} has no input {k!r}")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                if k in ("batch_size", "frame_load_cap") and v > LIMITS["max_frames"]:
                    p.append(f"node {nid}: {k}={v} over the {LIMITS['max_frames']}-frame limit")
                if k in ("width", "height", "custom_width", "custom_height") and v > LIMITS["max_side"]:
                    p.append(f"node {nid}: {k}={v} over the {LIMITS['max_side']} px limit")
                if k == "steps" and v > LIMITS["max_steps"]:
                    p.append(f"node {nid}: steps={v} over the limit of {LIMITS['max_steps']}")
            if (ct, k) in FILE_INPUTS and v not in names:
                p.append(f"node {nid}: {k}={v!r} is not one of the job's inputs")
            if ct == "SaveImage" and k == "filename_prefix" and not str(v).startswith(f"remake/{job.get('job_id')}/"):
                p.append(f"node {nid}: output prefix must start with remake/{job.get('job_id')}/")
            if isinstance(v, str) and (".." in v or v.startswith("/")):
                p.append(f"node {nid}: {k} looks like a filesystem path")
    for i in job.get("inputs", []):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", i.get("name", "")) or not re.fullmatch(r"[0-9a-f]{64}", i.get("sha256", "")):
            p.append(f"bad input entry {i.get('name')!r}")
    return p


def k_job(j): return f"jobs/{j}.json"
def k_claim(j): return f"claims/{j}.json"
def k_status(j): return f"status/{j}.json"
def k_cancel(j): return f"cancel/{j}"
def k_result(j, name): return f"results/{j}/{name}"
def k_worker(w): return f"workers/{w}.json"
def k_input(sha): return f"inputs/{sha}"
