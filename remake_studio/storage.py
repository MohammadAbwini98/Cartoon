"""Direct browser → R2 uploads and verified media storage.

  python3 -m remake_studio.storage prepare <name> <size_bytes>    → prints a time-limited upload link
  python3 -m remake_studio.storage complete <upload_id>           → completes, downloads, verifies SHA-256
  python3 -m remake_studio.storage check                          → self-test of both buckets

Two buckets (config/hybrid.toml): `media` (episode, generated frames; cloud credentials only) and the
job queue (cloud + Mac). The Mac never holds media-bucket credentials: jobs carry presigned GET links.

The upload page is stored in the media bucket and opened through a presigned GET link, so it is served
from the same origin as the presigned part-upload links it uses (no CORS rules needed). The browser
holds only links for this one object, valid until they expire; never account keys.
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import sys
import time
import tomllib

from remake_studio.hybrid.transport import S3Transport

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PART = 64 * 1024 * 1024       # R2/S3: equal parts ≥ 5 MiB except the last; max 10,000 parts


def media_store(env=os.environ, config="config/hybrid.toml") -> S3Transport:
    with open(config, "rb") as f:
        c = tomllib.load(f)["media"]
    missing = [v for v in (c["access_key_env"], c["secret_key_env"]) if not env.get(v)]
    if missing:
        raise SystemExit(f"media storage not configured: set {', '.join(missing)} as environment secrets")
    return S3Transport(c["endpoint"], c["bucket"], env[c["access_key_env"]], env[c["secret_key_env"]], c.get("region", "auto"))


def _state(state_dir, uid):
    d = os.path.join(state_dir, "uploads")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{uid}.json")


def prepare(store: S3Transport, name: str, size: int, state_dir: str = "state", expires: int = 3 * 86400,
            part_size: int = PART) -> dict:
    uid = time.strftime("%Y%m%d") + "-" + secrets.token_hex(4)
    ext = (os.path.splitext(name)[1] or ".bin").lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,5}", ext):
        ext = ".bin"
    key = f"media/source/{uid}/source{ext}"
    upload_id = store.create_multipart(key, "video/mp4" if ext == ".mp4" else "application/octet-stream")
    n = max(1, math.ceil(size / part_size))
    if n > 10000:
        raise ValueError("file too large for the part size")
    q = lambda i: f"partNumber={i}&uploadId={upload_id}"  # noqa: E731
    parts = [{"n": i + 1, "start": i * part_size, "end": min(size, (i + 1) * part_size),
              "url": store.presign("PUT", key, expires, q(i + 1))} for i in range(n)]
    cfg = {"id": uid, "name": name, "size": size, "key": key, "upload_id": upload_id, "expires": int(time.time()) + expires,
           "parts": parts, "manifest_url": store.presign("PUT", f"uploads/{uid}/manifest.json", expires)}
    with open(os.path.join(ROOT, "uploader", "direct.html"), encoding="utf-8") as f:
        page = f.read()
    with open(os.path.join(ROOT, "uploader", "transfer-core.js"), encoding="utf-8") as f:
        core = f.read()
    page = page.replace("/*__TRANSFER_CORE__*/", core).replace("/*__CONFIG__*/null", json.dumps(cfg, ensure_ascii=False))
    page_key = f"upload-pages/{uid}.html"
    store.put(page_key, page.encode("utf-8"), content_type="text/html; charset=utf-8")
    link = store.presign("GET", page_key, expires)
    rec = {"id": uid, "key": key, "upload_id": upload_id, "name": name, "size": size, "parts": n,
           "page_key": page_key, "expires": cfg["expires"], "status": "prepared"}
    with open(_state(state_dir, uid), "w") as f:
        json.dump(rec, f, indent=2)
    return {**rec, "link": link}


def complete(store: S3Transport, uid: str, state_dir: str = "state", verify_dir: str = "media/source") -> dict:
    with open(_state(state_dir, uid)) as f:
        rec = json.load(f)
    raw = store.get(f"uploads/{uid}/manifest.json")
    if raw is None:
        raise SystemExit("the browser has not finished: no manifest yet")
    m = json.loads(raw)
    if m.get("upload_id") != rec["upload_id"] or m.get("size") != rec["size"]:
        raise SystemExit("manifest does not match the prepared upload")
    nums = sorted(p["n"] for p in m["parts"])
    if nums != list(range(1, rec["parts"] + 1)) or any(not p.get("etag") for p in m["parts"]):
        raise SystemExit(f"manifest lists parts {nums}, expected 1..{rec['parts']}")
    if rec["status"] != "completed":
        store.complete_multipart(rec["key"], rec["upload_id"], [(p["n"], p["etag"]) for p in m["parts"]])
        rec["status"] = "completed"
    os.makedirs(os.path.join(verify_dir, uid), exist_ok=True)
    local = os.path.join(verify_dir, uid, "source" + os.path.splitext(rec["key"])[1])
    sha, n = store.download(rec["key"], local)
    ok = sha == m["sha256"] and n == rec["size"]
    rec.update(status="verified" if ok else "hash_mismatch", sha256_browser=m["sha256"], sha256_storage=sha,
               bytes_storage=n, original_name=m.get("name"), local_copy=local if ok else None)
    if ok:
        os.chmod(local, 0o444)
        store.delete(rec["page_key"])          # the upload page and its links are no longer needed
    else:
        os.remove(local)
    with open(_state(state_dir, uid), "w") as f:
        json.dump(rec, f, indent=2)
    return rec


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    if not a:
        print(__doc__); return 2
    if a[0] == "check":
        problems = media_store().selftest("selftest/")
        print("media bucket:", "OK" if not problems else problems)
        return 1 if problems else 0
    if a[0] == "prepare":
        r = prepare(media_store(), a[1], int(a[2]))
        print(json.dumps({k: r[k] for k in ("id", "parts", "expires")}, indent=2))
        print("upload link (send to the owner; expires with the parts):\n" + r["link"])
        return 0
    if a[0] == "complete":
        r = complete(media_store(), a[1])
        print(json.dumps(r, indent=2))
        return 0 if r["status"] == "verified" else 1
    print(__doc__); return 2


if __name__ == "__main__":
    sys.exit(main())
