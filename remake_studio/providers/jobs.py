"""Generation jobs: deterministic ids, budget gating, recovery, validation.

State machine (persisted after every transition, one JSON file per job):

  prepared ──submit──▶ submitted ──poll──▶ running ──▶ succeeded ──fetch──▶ validated
      │                    │                                     └─────────▶ rejected
      │                    └── unknown outcome ─▶ submit_unknown   (never auto-resubmitted)
      └── budget refused ─▶ blocked                failed ─(retryable, attempts left)─▶ prepared

* job id = hash of provider, model, canonical request and input file hashes,
  so asking for the same thing twice returns the same job instead of paying
  for it twice.
* A job that already has a remote id is recovered by polling that id; it is
  never resubmitted while that id might still produce a result.
* Billable providers reserve the estimate in the budget ledger before every
  attempt; the reservation is settled with the reported cost (or the estimate
  when the provider reports none) or released when nothing was charged.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import time

from remake_studio.media import validate
from remake_studio.providers.budget import Budget, BudgetError
from remake_studio.providers.http import HttpError, UnknownOutcome

TERMINAL_REMOTE = ("succeeded", "failed", "canceled")


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def fingerprint(provider: str, model: str, request: dict, input_files: list[str]) -> str:
    canon = json.dumps({"provider": provider, "model": model, "request": request,
                        "inputs": [validate.sha256(p) for p in input_files]}, sort_keys=True, separators=(",", ":"))
    return "j_" + hashlib.sha256(canon.encode()).hexdigest()[:20]


class JobStore:
    def __init__(self, state_dir: str):
        self.dir = os.path.join(state_dir, "jobs")
        os.makedirs(self.dir, exist_ok=True)

    def path(self, job_id: str) -> str:
        return os.path.join(self.dir, f"{job_id}.json")

    def load(self, job_id: str) -> dict | None:
        p = self.path(job_id)
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def save(self, job: dict):
        job["updated"] = _now()
        tmp = self.path(job["id"]) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(job, f, indent=2)
        os.replace(tmp, self.path(job["id"]))

    def all(self) -> list[dict]:
        return [self.load(n[:-5]) for n in sorted(os.listdir(self.dir)) if n.endswith(".json")]


class Runner:
    def __init__(self, providers: dict, state_dir: str, output_dir: str, budget: Budget | None = None,
                 poll_seconds: float = 5.0, timeout_seconds: float = 3600.0, sleep=time.sleep):
        self.providers = providers
        self.store = JobStore(state_dir)
        self.budget = budget or Budget(state_dir)
        self.out = output_dir
        self.poll, self.timeout, self.sleep = poll_seconds, timeout_seconds, sleep

    def create(self, provider: str, model: str, request: dict, input_files=(), expect: dict | None = None,
               shot: str | None = None, max_attempts: int = 2) -> dict:
        jid = fingerprint(provider, model, request, list(input_files))
        job = self.store.load(jid)
        if job:
            return job
        job = {"id": jid, "provider": provider, "model": model, "request": request, "shot": shot,
               "inputs": [{"path": p, "sha256": validate.sha256(p)} for p in input_files],
               "expect": expect or {}, "state": "prepared", "remote_id": None, "attempts": [],
               "max_attempts": max_attempts, "outputs": [], "cost": None, "created": _now()}
        self.store.save(job)
        return job

    # ------------------------------------------------------------------ run
    def run(self, job_id: str) -> dict:
        job = self.store.load(job_id)
        p = self.providers[job["provider"]]
        billable = getattr(p, "billable", p.kind == "paid_api")
        while True:
            st = job["state"]
            if st in ("validated", "rejected", "blocked", "submit_unknown"):
                return job
            if st == "failed":
                if len(job["attempts"]) < job["max_attempts"] and job.get("retryable"):
                    job["state"] = "prepared"; self.store.save(job); continue
                return job
            if st == "prepared":
                cost = p.estimate(job["model"], job["request"])
                attempt_key = f"{job['id']}#{len(job['attempts']) + 1}"
                if billable:
                    try:
                        self.budget.reserve(attempt_key, p.name, job["model"], cost)
                    except BudgetError as e:
                        job.update(state="blocked", blocked_reason=str(e)); self.store.save(job); return job
                job["cost"] = {"estimate": cost.__dict__ if cost else None, "attempt_key": attempt_key}
                try:
                    rid = p.submit(job["model"], job["request"])
                except UnknownOutcome as e:
                    job["attempts"].append({"ts": _now(), "remote_id": None, "outcome": "unknown", "error": str(e)})
                    job["state"] = "submit_unknown"; self.store.save(job); return job
                except HttpError as e:
                    job["attempts"].append({"ts": _now(), "remote_id": None, "outcome": "rejected", "error": str(e)})
                    if billable:
                        self.budget.release(attempt_key, "submit rejected, not started")
                    job.update(state="failed", retryable=e.status in (429, 503)); self.store.save(job); continue
                job["attempts"].append({"ts": _now(), "remote_id": rid, "outcome": "submitted"})
                job.update(state="submitted", remote_id=rid); self.store.save(job)
            if job["state"] in ("submitted", "running"):
                job = self._poll(job, p, billable)
                if job["state"] in ("submitted", "running"):
                    return job          # still in progress after the wait budget; call run() again later

    def _poll(self, job, p, billable):
        deadline = time.monotonic() + self.timeout
        while True:
            s = p.status(job["remote_id"])
            job["progress"] = s.progress
            if s.state not in TERMINAL_REMOTE:
                if job["state"] != "running":
                    job["state"] = "running"; self.store.save(job)
                if time.monotonic() > deadline:
                    job["timed_out_waiting"] = True; self.store.save(job); return job
                self.sleep(self.poll)
                continue
            key = job["cost"]["attempt_key"] if job.get("cost") else None
            est = (job.get("cost") or {}).get("estimate") or {}
            if s.state == "succeeded":
                job_dir = os.path.join(self.out, job["id"])
                os.makedirs(job_dir, exist_ok=True)
                files = p.fetch(job["remote_id"], s, job_dir)
                problems_all = []
                job["outputs"] = []
                for f in files:
                    pr, problems = validate.check(f, job["expect"]) if f.lower().endswith(
                        (".mp4", ".mov", ".webm", ".png", ".jpg", ".jpeg", ".webp", ".gif")) else ({}, [])
                    job["outputs"].append({"path": f, "sha256": validate.sha256(f), "probe": pr, "problems": problems})
                    problems_all += problems
                if not files:
                    problems_all.append("provider reported success but returned no files")
                if billable and key:
                    actual = s.raw.get("cost") if isinstance(s.raw.get("cost"), (int, float)) else None
                    self.budget.settle(key, actual if actual is not None else est.get("amount", 0.0),
                                       "reported" if actual is not None else "estimate (provider reported no cost)")
                job["attempts"][-1]["outcome"] = "succeeded"
                job["state"] = "rejected" if problems_all else "validated"
                job["problems"] = problems_all
            else:
                job["attempts"][-1].update(outcome=s.state, error=s.error)
                if billable and key:
                    # billing for failed runs is provider-specific; count the estimate until reconciled
                    self.budget.settle(key, est.get("amount", 0.0), f"{s.state}; billing unconfirmed")
                job.update(state="failed", retryable=s.state == "failed", last_error=s.error)
            self.store.save(job)
            return job
