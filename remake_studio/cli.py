"""remake — command line for the Animation Remake Studio.

  remake breakdown <episode.mp4> <out_dir> [...]     shot / transition analysis
  remake ingest <chunk_dir> <out_file> [...]          rebuild an uploaded file
  remake providers [--discover]                       configured backends (keys from env)
  remake budget                                       spending authorization and ledger totals
  remake jobs                                         generation jobs and their states
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
    print(f"unknown command {cmd!r}\n{__doc__}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
