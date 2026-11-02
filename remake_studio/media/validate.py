"""Probe and check generated media against what the manifest expects."""
from __future__ import annotations

import hashlib
import json
import subprocess
from fractions import Fraction


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def probe(path: str) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                        "-count_frames", path], capture_output=True, text=True)
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr.strip()[-300:]}
    info = json.loads(r.stdout)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    out = {"ok": True, "format": info.get("format", {}).get("format_name"),
           "duration_s": float(info.get("format", {}).get("duration", 0) or 0)}
    if v:
        rate = v.get("avg_frame_rate", "0/0")
        out.update(width=v.get("width"), height=v.get("height"), codec=v.get("codec_name"),
                   pix_fmt=v.get("pix_fmt"), frames=int(v.get("nb_read_frames") or 0),
                   fps=str(Fraction(rate)) if rate not in ("0/0", "") else None)
    if a:
        out.update(audio_codec=a.get("codec_name"), sample_rate=int(a.get("sample_rate") or 0))
    return out


def check(path: str, expect: dict | None) -> tuple[dict, list[str]]:
    """Return (probe, problems). `expect` keys: width, height, frames, fps,
    min_width, min_height, has_audio. Empty problems = accepted."""
    p = probe(path)
    problems = []
    if not p.get("ok"):
        return p, [f"unreadable: {p.get('error')}"]
    e = expect or {}
    for k in ("width", "height", "frames"):
        if k in e and p.get(k) != e[k]:
            problems.append(f"{k} {p.get(k)} != expected {e[k]}")
    if "fps" in e and p.get("fps") != str(Fraction(e["fps"])):
        problems.append(f"fps {p.get('fps')} != expected {e['fps']}")
    if "min_width" in e and (p.get("width") or 0) < e["min_width"]:
        problems.append(f"width {p.get('width')} below {e['min_width']}")
    if "min_height" in e and (p.get("height") or 0) < e["min_height"]:
        problems.append(f"height {p.get('height')} below {e['min_height']}")
    if e.get("has_audio") and not p.get("audio_codec"):
        problems.append("no audio stream")
    return p, problems
