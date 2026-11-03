"""One production snapshot across every backend: workers, jobs, compatibility, budget.

The orchestrator writes it to the production page's database (`production/current`);
the page renders it. Job rows come from the common JobStore whatever ran them.
"""
from __future__ import annotations

import json
import os
import time
import tomllib

from remake_studio.hybrid import protocol as P
from remake_studio.hybrid.mac_provider import MacWorkerQueue
from remake_studio.hybrid.scheduler import Compat
from remake_studio.hybrid.transport import from_config
from remake_studio.providers.budget import Budget
from remake_studio.providers.jobs import JobStore


def load_mac(config_path: str = "config/hybrid.toml", env=os.environ) -> tuple[MacWorkerQueue | None, str]:
    try:
        with open(config_path, "rb") as f:
            cfg = tomllib.load(f)
    except FileNotFoundError:
        return None, "config/hybrid.toml missing"
    t, k = cfg.get("transport", {}), cfg.get("signing_key_env", "REMAKE_JOB_SIGNING_KEY")
    missing = [v for v in (t.get("access_key_env"), t.get("secret_key_env"), k) if v and not env.get(v)]
    if t.get("type") == "s3" and missing:
        return None, f"not configured (set {', '.join(missing)} as environment secrets)"
    if not env.get(k):
        return None, f"not configured (set {k})"
    return MacWorkerQueue(from_config(t, env), env[k].encode(), cfg.get("worker_id", "mac-m3pro"),
                          heartbeat_max_age=cfg.get("heartbeat_max_age_s", 120)), "configured"


def snapshot(state_dir: str, mac: MacWorkerQueue | None, mac_note: str = "") -> dict:
    now = time.time()
    workers = []
    if mac is not None:
        hb = mac.heartbeat()
        workers.append({"id": mac.worker_id, "backend": "mac_mps", "online": mac.online(),
                        "last_seen_s": round(now - hb["ts"], 1) if hb else None, "busy": (hb or {}).get("busy"),
                        "hardware": (hb or {}).get("hardware"), "comfyui": (hb or {}).get("comfyui")})
    else:
        workers.append({"id": "mac-m3pro", "backend": "mac_mps", "online": False, "note": mac_note})
    jobs = []
    for j in JobStore(state_dir).all():
        row = {"id": j["id"], "backend": j["provider"], "state": j["state"], "shot": j.get("shot"),
               "model": j["model"], "created": j.get("created"), "updated": j.get("updated"),
               "progress": j.get("progress"), "attempts": len(j["attempts"]),
               "reason": j.get("blocked_reason") or j.get("last_error"), "remote_id": j.get("remote_id")}
        if j["provider"] == "mac" and mac is not None and j.get("remote_id"):
            b = mac.store.get(P.k_status(j["remote_id"]))
            if b:
                s = json.loads(b)
                row.update(worker_state=s.get("state"), phase=s.get("phase"), metrics=s.get("metrics"),
                           error_kind=s.get("error_kind"))
        jobs.append(row)
    return {"generated": now, "workers": workers, "jobs": jobs, "compat": Compat(state_dir).all(),
            "budget": {"authorization": Budget(state_dir).authorization(), "ledger": Budget(state_dir).totals()}}
