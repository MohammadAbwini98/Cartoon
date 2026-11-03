"""MPS compatibility benchmark: staged plan, measured metrics, compatibility evidence.

Stages (each runs only if the previous one succeeded):
  B0 smoke_v1       one image through LoadImage → SaveImage: node loading, upload, output retrieval
  B1 keyframes_v1   six representative source frames, redesigned per frame (no motion module)
  B2 pilot_shot_v1  48 frames (2 s at 24 fps) with AnimateDiff + ControlNet + IPAdapter
  B3 pilot_shot_v1  96 frames (4 s), same settings
Never the full episode.

Measured per stage (worker): peak system memory in use, peak ComfyUI process RSS, wall time,
seconds per frame, effective frames per second, failure kind (oom / execution / comfyui_exited).
Measured here from the frames (proxies, labelled as such):
  flicker_ratio        mean |O_t − O_t−1| / mean |S_t − S_t−1|   (1 ≈ output changes as much as the source)
  hold_instability     mean |O_t − O_t−1| on frames where the source holds still (0 = no boiling)
  motion_correlation   correlation of per-frame change energy, output vs source (timing adherence)
Output quality and identity are reviewed side by side (QUALITY_ACCEPTANCE_MATRIX R-criteria);
`quality_equivalent` is set only from that review.
"""
from __future__ import annotations

import os
import subprocess

import numpy as np

from remake_studio.hybrid.scheduler import Compat

STAGES = [
    {"id": "B0", "template": "smoke_v1", "frames": 1, "purpose": "first contact"},
    {"id": "B1", "template": "keyframes_v1", "frames": 6, "purpose": "six representative frames"},
    {"id": "B2", "template": "pilot_shot_v1", "frames": 48, "purpose": "2 s animation"},
    {"id": "B3", "template": "pilot_shot_v1", "frames": 96, "purpose": "4 s animation"},
]


def smoke_graph(job_image: str) -> dict:
    return {"1": {"class_type": "LoadImage", "inputs": {"image": job_image}},
            "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "remake/x/frame"}}}


def next_stage(results: dict) -> dict | None:
    """results: {"B0": "succeeded", ...} → the next stage to run, or None when done or blocked."""
    for s in STAGES:
        r = results.get(s["id"])
        if r is None:
            return s
        if r != "succeeded":
            return None
    return None


def _gray(paths_or_clip, w=128, h=72) -> np.ndarray:
    if isinstance(paths_or_clip, str):
        src = ["-i", paths_or_clip]
    else:
        lst = paths_or_clip
        src = ["-f", "concat", "-safe", "0", "-i", lst]
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", *src, "-vf",
                        f"scale={w}:{h}:flags=area,format=gray", "-fps_mode", "passthrough", "-f", "rawvideo", "-"],
                       capture_output=True, check=True)
    n = len(r.stdout) // (w * h)
    return np.frombuffer(r.stdout[: n * w * h], np.uint8).reshape(n, h, w).astype(np.float32) / 255.0


def frames_list(paths: list[str], out_txt: str) -> str:
    with open(out_txt, "w") as f:
        for p in sorted(paths):
            f.write(f"file '{os.path.abspath(p)}'\n")
    return out_txt


def temporal_metrics(output: np.ndarray, source: np.ndarray, hold_threshold: float = 0.5 / 255) -> dict:
    n = min(len(output), len(source))
    if n < 3:
        return {"frames_compared": n, "note": "too few frames for temporal metrics"}
    o, s = output[:n], source[:n]
    do = np.abs(np.diff(o, axis=0)).mean(axis=(1, 2))
    ds = np.abs(np.diff(s, axis=0)).mean(axis=(1, 2))
    holds = ds < hold_threshold
    corr = float(np.corrcoef(do, ds)[0, 1]) if do.std() > 0 and ds.std() > 0 else None
    return {"frames_compared": n,
            "flicker_ratio": round(float(do.mean() / max(ds.mean(), 1e-6)), 3),
            "hold_frames": int(holds.sum()),
            "hold_instability": round(float(do[holds].mean()), 5) if holds.any() else None,
            "motion_correlation": round(corr, 3) if corr is not None else None}


def record(compat: Compat, stage: dict, status: dict, temporal: dict | None, width: int, height: int,
           mac_limit_gb: float = 14.4) -> dict:
    """Turn one benchmark result into compatibility evidence. Only a successful run within the memory
    limit can validate; quality stays unconfirmed until the side-by-side review sets it."""
    m = status.get("metrics") or {}
    peak_gb = round(max(m.get("peak_system_used_mb") or 0, m.get("peak_comfyui_rss_mb") or 0) / 1024, 2)
    ok = status.get("state") == "succeeded" and m.get("output_frames") == stage["frames"] and peak_gb <= mac_limit_gb
    fields = {"status": "validated" if ok else "failed", "peak_memory_gb": peak_gb,
              "sec_per_frame": m.get("sec_per_frame"), "effective_fps": m.get("effective_fps"),
              "max_frames_tested": stage["frames"] if ok else compat.get(stage["template"], "mps").get("max_frames_tested", 0),
              "width": width, "height": height, "last_stage": stage["id"], "temporal": temporal,
              "failure": None if ok else {"state": status.get("state"), "kind": status.get("error_kind"),
                                          "error": (status.get("error") or "")[:300], "peak_memory_gb": peak_gb},
              "evidence": [status.get("job_id")]}
    if not ok:
        fields.pop("max_frames_tested")
    fields.setdefault("quality_equivalent", compat.get(stage["template"], "mps").get("quality_equivalent"))
    compat.record(stage["template"], "mps", **fields)
    return compat.get(stage["template"], "mps")
