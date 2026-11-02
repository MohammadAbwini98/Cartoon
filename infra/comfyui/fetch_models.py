"""Download the pilot models onto a volume, verifying size and SHA-256.

  python3 infra/comfyui/fetch_models.py <models_root> [--manifest infra/comfyui/models.json] [--only name ...]

Files land at <models_root>/<folder>/<name> (on RunPod serverless the root is
/runpod-volume/models). A file that already has the right hash is skipped. A
download is written to <name>.part and renamed only after its hash matches; a
mismatch deletes the part file and fails. Entries without a sha256 (an
unchosen checkpoint) are refused, never downloaded blind.
"""
import argparse
import hashlib
import json
import os
import sys
import urllib.request


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def fetch(entry, root, opener=urllib.request.urlopen):
    if not entry.get("url") or not entry.get("sha256"):
        raise ValueError(f"{entry['name']}: no pinned url/sha256 yet ({entry.get('note', '')})")
    dest = os.path.join(root, entry["folder"], entry["name"])
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest) and sha256(dest) == entry["sha256"]:
        return dest, "present"
    part = dest + ".part"
    h = hashlib.sha256()
    size = 0
    try:
        with opener(entry["url"]) as r, open(part, "wb") as f:
            for b in iter(lambda: r.read(1 << 22), b""):
                f.write(b); h.update(b); size += len(b)
        if entry.get("bytes") is not None and size != entry["bytes"]:
            raise ValueError(f"{entry['name']}: {size} bytes, expected {entry['bytes']}")
        if h.hexdigest() != entry["sha256"]:
            raise ValueError(f"{entry['name']}: sha256 {h.hexdigest()} != pinned {entry['sha256']}")
        os.replace(part, dest)
    finally:
        if os.path.exists(part):
            os.remove(part)
    return dest, "downloaded"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root")
    ap.add_argument("--manifest", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "models.json"))
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args(argv)
    entries = json.load(open(a.manifest))["models"]
    failed = 0
    for e in entries:
        if a.only and e["name"] not in a.only:
            continue
        try:
            path, how = fetch(e, a.root)
            print(f"ok   {how:10s} {path}")
        except Exception as ex:
            failed += 1
            print(f"FAIL {e['name']}: {ex}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
