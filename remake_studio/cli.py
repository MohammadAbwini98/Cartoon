"""remake — command line for the Animation Remake Studio.

  remake breakdown <episode.mp4> <out_dir> [...]     shot / transition analysis
  remake ingest <chunk_dir> <out_file> [...]          rebuild an uploaded file
  remake providers [--discover]                       configured backends (keys from env)
  remake budget                                       spending authorization and ledger totals
  remake jobs                                         generation jobs and their states
  remake hybrid status [--json FILE]                  workers, jobs on every backend, MPS evidence, budget
  remake hybrid decide TEMPLATE FRAMES WxH [--benchmark] [--fallback]   where a render would run, and why
"""
from __future__ import annotations

import json
import os
import sys

STATE = os.environ.get("REMAKE_STATE", "state")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "breakdown":
        from remake_studio.preprocess import breakdown
        return breakdown.main(rest)
    if cmd == "ingest":
        from remake_studio.ingest import reassemble
        return reassemble.main(rest)
    if cmd == "providers":
        from remake_studio.providers import registry
        providers, report = registry.build()
        for r in report:
            line = f"{r['provider']:10s} {r['status']}"
            if r.get("missing_env"):
                line += f"  (set {', '.join(r['missing_env'])} as environment secrets)"
            elif "billable" in r:
                line += "  billable" if r["billable"] else "  not metered"
            print(line)
        if "--discover" in rest and "comfyui" in providers:
            print(json.dumps(providers["comfyui"].node_packs(), indent=2))
        return 0
    if cmd == "budget":
        from remake_studio.providers.budget import Budget
        b = Budget(STATE)
        a = b.authorization()
        print("authorization:", json.dumps(a, indent=2) if a else "none (paid calls disabled)")
        print("ledger:", json.dumps(b.totals(), indent=2))
        return 0
    if cmd == "jobs":
        from remake_studio.providers.jobs import JobStore
        for j in JobStore(STATE).all():
            print(f"{j['id']}  {j['provider']:10s} {j['state']:15s} shot={j.get('shot')} "
                  f"attempts={len(j['attempts'])} {j.get('blocked_reason') or j.get('last_error') or ''}")
        return 0
    if cmd == "hybrid":
        from remake_studio.hybrid import dashboard
        from remake_studio.hybrid.scheduler import Scheduler, Task
        from remake_studio.providers import registry
        from remake_studio.providers.budget import Budget
        mac, note = dashboard.load_mac()
        if rest and rest[0] == "status":
            snap = dashboard.snapshot(STATE, mac, note)
            if "--json" in rest:
                with open(rest[rest.index("--json") + 1], "w") as f:
                    json.dump(snap, f, indent=2)
            for w in snap["workers"]:
                print(f"worker {w['id']}: {'online' if w['online'] else 'offline'} {w.get('note') or ''}")
            print(f"jobs: {len(snap['jobs'])}; MPS evidence: {list(snap['compat']) or 'none yet'}")
            return 0
        if rest and rest[0] == "decide":
            w, h = map(int, rest[3].split("x"))
            providers, _ = registry.build()
            cloud = {k: v for k, v in providers.items() if k in ("runpod", "comfyui")}
            apis = {k: v for k, v in providers.items() if k in ("fal", "replicate")}
            s = Scheduler(STATE, mac, cloud, apis, Budget(STATE))
            d = s.decide(Task("comfy_workflow", rest[1], int(rest[2]), w, h,
                              purpose="benchmark" if "--benchmark" in rest else "production",
                              allow_fallback="--fallback" in rest))
            print(json.dumps(d.__dict__, indent=2))
            return 0
    print(f"unknown command {cmd!r}\n{__doc__}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
