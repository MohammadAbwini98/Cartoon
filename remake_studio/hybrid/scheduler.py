"""Hybrid scheduler: Mac (MPS) → cloud NVIDIA → commercial API, plus local CPU.

Rules
* CPU-suitable tasks (preprocess, encode, assemble, analysis) run on the cloud container's CPU.
* A ComfyUI template runs on the Mac only with evidence: a compatibility record for
  (template, "mps") with status "validated", measured peak memory within the Mac's limit,
  and quality marked equivalent. Benchmark jobs are the exception: they exist to produce
  that evidence, and they may only run on the Mac.
  A CUDA result never implies an MPS result.
* Prefer the Mac when eligible. If it is offline, the job waits in the queue unless the task
  allows fallback and a cloud backend is configured and inside an authorized budget.
* Every decision returns its reason and why each alternative was not used.

state/compat.json (written from benchmark evidence, never by hand-waving):
  {"pilot_shot_v1/mps": {"status": "validated" | "failed" | "untested", "peak_memory_gb": 11.2,
     "sec_per_frame": 9.4, "max_frames_tested": 96, "width": 512, "height": 288,
     "quality_equivalent": true | false | null, "evidence": ["<job id>", ...], "updated": "..."}}
"""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field

from remake_studio.providers.budget import BudgetError

CPU_TASKS = ("preprocess", "encode", "assemble", "analysis")


@dataclass
class Task:
    kind: str                         # "comfy_workflow" | "api_generation" | one of CPU_TASKS
    template: str | None = None
    frames: int = 0
    width: int = 0
    height: int = 0
    purpose: str = "production"       # or "benchmark"
    allow_fallback: bool = False      # may leave the Mac queue for a paid backend
    api_model: str | None = None


@dataclass
class Decision:
    backend: str | None               # "mac" | "cloud:<provider>" | "api:<provider>" | "cpu" | None (wait)
    action: str                       # "run" | "queue_for_mac" | "blocked"
    reason: str
    rejected: dict = field(default_factory=dict)
    estimate: dict = field(default_factory=dict)


class Compat:
    def __init__(self, state_dir: str):
        self.path = os.path.join(state_dir, "compat.json")

    def all(self) -> dict:
        try:
            with open(self.path) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def get(self, template: str, variant: str) -> dict:
        return self.all().get(f"{template}/{variant}", {"status": "untested"})

    def record(self, template: str, variant: str, **fields):
        d = self.all()
        cur = d.get(f"{template}/{variant}", {"status": "untested", "evidence": []})
        ev = cur.get("evidence", []) + fields.pop("evidence", [])
        cur.update(fields, evidence=ev, updated=dt.datetime.now(dt.timezone.utc).isoformat())
        d[f"{template}/{variant}"] = cur
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, self.path)


class Scheduler:
    def __init__(self, state_dir: str, mac=None, cloud: dict | None = None, apis: dict | None = None,
                 budget=None, mac_memory_gb: float = 18.0, mac_memory_fraction: float = 0.8):
        self.compat = Compat(state_dir)
        self.mac, self.cloud, self.apis, self.budget = mac, cloud or {}, apis or {}, budget
        self.mac_limit_gb = mac_memory_gb * mac_memory_fraction

    def _mac_check(self, t: Task) -> tuple[bool, str, dict]:
        if self.mac is None:
            return False, "no Mac worker configured", {}
        c = self.compat.get(t.template, "mps")
        if t.purpose == "benchmark":
            return True, "benchmark job (produces the MPS evidence)", {}
        if c.get("status") != "validated":
            return False, f"{t.template} on MPS is {c.get('status', 'untested')}; benchmark it first", {}
        if c.get("quality_equivalent") is not True:
            return False, "MPS output quality not yet confirmed equivalent", {}
        scale = (t.frames / max(1, c.get("max_frames_tested", 0))) if t.frames else 1.0
        if t.frames > c.get("max_frames_tested", 0):
            return False, f"{t.frames} frames exceeds the {c.get('max_frames_tested')} validated on MPS", {}
        if (t.width * t.height) > c.get("width", 0) * c.get("height", 0):
            return False, f"{t.width}x{t.height} larger than validated {c.get('width')}x{c.get('height')}", {}
        if c.get("peak_memory_gb", 1e9) > self.mac_limit_gb:
            return False, f"measured peak {c.get('peak_memory_gb')} GB exceeds the {self.mac_limit_gb:.1f} GB limit", {}
        est = {"seconds": round(c.get("sec_per_frame", 0) * t.frames, 1), "cost_usd": 0.0, "scale": round(scale, 2)}
        return True, "validated on MPS within memory and size limits", est

    def _paid(self, providers: dict, label: str, model: str, request: dict) -> tuple[str | None, str, dict, dict]:
        rejected = {}
        options = []
        for name, p in providers.items():
            cost = p.estimate(model, request)
            if self.budget is None:
                rejected[f"{label}:{name}"] = "no budget configured"
                continue
            try:
                self.budget.check(p.name, cost)
            except BudgetError as e:
                rejected[f"{label}:{name}"] = str(e)
                continue
            options.append((cost.amount, name, cost))
        if not options:
            return None, "", rejected, {}
        options.sort()
        amount, name, cost = options[0]
        return f"{label}:{name}", f"cheapest authorized {label} backend", rejected, {"cost_usd": amount, "basis": cost.basis}

    def decide(self, t: Task, request: dict | None = None) -> Decision:
        request = request or {}
        if t.kind in CPU_TASKS:
            return Decision("cpu", "run", f"{t.kind} runs on the cloud container CPU")
        if t.kind == "api_generation":
            b, why, rej, est = self._paid(self.apis, "api", t.api_model, request)
            return Decision(b, "run" if b else "blocked", why or "no authorized API backend", rej, est)
        rejected = {}
        ok, why, est = self._mac_check(t)
        if ok:
            if self.mac.online():
                return Decision("mac", "run", why, rejected, est)
            if t.purpose == "benchmark" or not t.allow_fallback:
                return Decision("mac", "queue_for_mac", f"{why}; Mac offline, job waits in the queue", rejected, est)
            rejected["mac"] = "offline; fallback allowed for this task"
        else:
            rejected["mac"] = why
            if t.purpose == "benchmark":
                return Decision(None, "blocked", "benchmark jobs exist to test the Mac and run nowhere else", rejected)
        b, pwhy, rej, pest = self._paid(self.cloud, "cloud", t.template, request)
        rejected.update(rej)
        if b:
            return Decision(b, "run", pwhy, rejected, pest)
        if ok:
            return Decision("mac", "queue_for_mac", "Mac offline and no authorized cloud fallback; waiting for the Mac",
                            rejected, est)
        return Decision(None, "blocked", "no eligible backend: validate on MPS or authorize a cloud GPU budget", rejected)
