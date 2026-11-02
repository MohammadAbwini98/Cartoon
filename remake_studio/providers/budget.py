"""Spending authorization and an append-only cost ledger.

Nothing billable runs unless an authorization file exists, names the provider,
has not expired, and leaves room for the job's estimate. An unknown estimate
is refused: unknown cost is not free. Every reservation, settlement and
release is appended to the ledger, so spend survives restarts and can be
audited. The authorization file is written only from an explicit user
instruction (record who gave it and when); this module never creates one.

authorization.json:
  {"authorized_by": "user message 2026-11-02", "currency": "USD",
   "total_cap": 25.0, "per_job_cap": 3.0, "providers": ["fal", "runpod"],
   "scope": "pilot G2: hardest 2-4 s, <= 6 candidates", "expires": "2026-11-09"}
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading

from remake_studio.providers.base import Cost


class BudgetError(Exception):
    pass


class Budget:
    def __init__(self, state_dir: str, now=lambda: dt.datetime.now(dt.timezone.utc)):
        self.dir = state_dir
        self.auth_path = os.path.join(state_dir, "authorization.json")
        self.ledger_path = os.path.join(state_dir, "ledger.jsonl")
        self.now = now
        self._lock = threading.Lock()
        os.makedirs(state_dir, exist_ok=True)

    # ------------------------------------------------------------ reading
    def authorization(self) -> dict | None:
        if not os.path.exists(self.auth_path):
            return None
        with open(self.auth_path, encoding="utf-8") as f:
            return json.load(f)

    def entries(self) -> list[dict]:
        if not os.path.exists(self.ledger_path):
            return []
        with open(self.ledger_path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def totals(self) -> dict:
        """committed = settled actual spend + open reservations (worst case)."""
        reserved, settled = {}, 0.0
        for e in self.entries():
            if e["kind"] == "reserve":
                reserved[e["job_id"]] = e["amount"]
            elif e["kind"] == "settle":
                settled += e["amount"]
                reserved.pop(e["job_id"], None)
            elif e["kind"] == "release":
                reserved.pop(e["job_id"], None)
        return {"settled": round(settled, 6), "reserved": round(sum(reserved.values()), 6),
                "committed": round(settled + sum(reserved.values()), 6), "open": sorted(reserved)}

    # ------------------------------------------------------------ writing
    def _append(self, entry: dict):
        entry = {"ts": self.now().isoformat(), **entry}
        with open(self.ledger_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def check(self, provider: str, cost: Cost | None) -> dict:
        """Raise BudgetError unless this spend is authorized. Returns the authorization."""
        a = self.authorization()
        if a is None:
            raise BudgetError("no spending authorization on file; paid calls are disabled")
        if provider not in a.get("providers", []):
            raise BudgetError(f"provider {provider!r} is not in the authorized list {a.get('providers')}")
        exp = a.get("expires")
        if exp and self.now().date() > dt.date.fromisoformat(exp):
            raise BudgetError(f"authorization expired on {exp}")
        if cost is None:
            raise BudgetError("cost estimate unknown; unknown cost is not authorized")
        if cost.currency != a.get("currency"):
            raise BudgetError(f"estimate in {cost.currency}, authorization in {a.get('currency')}")
        if cost.amount > a.get("per_job_cap", 0):
            raise BudgetError(f"estimate {cost.amount} exceeds per-job cap {a.get('per_job_cap')}")
        t = self.totals()
        if t["committed"] + cost.amount > a.get("total_cap", 0):
            raise BudgetError(f"estimate {cost.amount} would exceed total cap {a.get('total_cap')} "
                              f"(committed {t['committed']})")
        return a

    def reserve(self, job_id: str, provider: str, model: str, cost: Cost | None):
        with self._lock:
            self.check(provider, cost)
            self._append({"kind": "reserve", "job_id": job_id, "provider": provider, "model": model,
                          "amount": cost.amount, "currency": cost.currency, "basis": cost.basis})

    def settle(self, job_id: str, amount: float, note: str = ""):
        with self._lock:
            self._append({"kind": "settle", "job_id": job_id, "amount": amount, "note": note})

    def release(self, job_id: str, note: str = ""):
        with self._lock:
            self._append({"kind": "release", "job_id": job_id, "amount": 0.0, "note": note})
