"""Rebuild a file uploaded through the browser uploader from its RMSC1 chunks.

    python3 -m remake_studio.ingest.reassemble <chunk_dir> <out_file>
        [--transfer-id ID] [--expect-sha256 HEX] [--expect-size BYTES]

Chunks are identified by their header, never by file name, so the directory
can hold them under any names (asset ids, download order). Every chunk's
length and SHA-256 are checked, all indices 0..total-1 must be present once,
the whole file is hashed while it is written, and the result is renamed into
place only after every check passes. The output is made read-only. An
existing output is never overwritten unless it already has the expected hash.
Format: see uploader/transfer-core.js.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from dataclasses import dataclass

MAGIC = b"RMSC1 "


class TransferError(Exception):
    pass


@dataclass
class Chunk:
    path: str
    transfer_id: str
    index: int
    total: int
    payload_len: int
    sha256: str
    enc: str
    header_len: int

    def payload(self) -> bytes:
        with open(self.path, "rb") as f:
            f.seek(self.header_len)
            body = f.read()
        if self.enc == "b64":
            try:
                body = base64.b64decode(body, validate=True)
            except ValueError as e:
                raise TransferError(f"chunk {self.index}: invalid base64 ({e})") from None
        elif self.enc != "bin":
            raise TransferError(f"chunk {self.index}: unknown encoding {self.enc!r}")
        if len(body) != self.payload_len:
            raise TransferError(f"chunk {self.index}: {len(body)} bytes, header says {self.payload_len}")
        if hashlib.sha256(body).hexdigest() != self.sha256:
            raise TransferError(f"chunk {self.index}: SHA-256 mismatch (corrupted or truncated)")
        return body


def read_header(path: str) -> Chunk | None:
    with open(path, "rb") as f:
        head = f.read(256)
    if not head.startswith(MAGIC):
        return None
    nl = head.find(b"\n")
    if nl < 0:
        raise TransferError(f"{path}: RMSC1 header has no newline")
    parts = head[:nl].decode("ascii").split(" ")
    if len(parts) != 7:
        raise TransferError(f"{path}: malformed RMSC1 header")
    _, tid, idx, total, plen, sha, enc = parts
    return Chunk(path, tid, int(idx), int(total), int(plen), sha.lower(), enc, nl + 1)


def scan(chunk_dir: str) -> tuple[dict[str, list[Chunk]], list[str]]:
    groups: dict[str, list[Chunk]] = {}
    skipped = []
    for name in sorted(os.listdir(chunk_dir)):
        p = os.path.join(chunk_dir, name)
        if not os.path.isfile(p):
            continue
        c = read_header(p)
        if c is None:
            skipped.append(name)
        else:
            groups.setdefault(c.transfer_id, []).append(c)
    return groups, skipped


def plan(chunks: list[Chunk]) -> list[Chunk]:
    totals = {c.total for c in chunks}
    if len(totals) != 1:
        raise TransferError(f"chunks disagree on the chunk count: {sorted(totals)}")
    total = totals.pop()
    by_index: dict[int, Chunk] = {}
    for c in chunks:
        if not 0 <= c.index < total:
            raise TransferError(f"chunk index {c.index} outside 0..{total - 1}")
        prev = by_index.get(c.index)
        if prev and prev.sha256 != c.sha256:
            raise TransferError(f"two different chunks claim index {c.index}")
        by_index.setdefault(c.index, c)          # identical re-uploads are harmless
    missing = [i for i in range(total) if i not in by_index]
    if missing:
        shown = ", ".join(map(str, missing[:20])) + (" …" if len(missing) > 20 else "")
        raise TransferError(f"{len(missing)} of {total} chunks missing: {shown}")
    return [by_index[i] for i in range(total)]


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def reassemble(chunk_dir: str, out_file: str, transfer_id: str | None = None,
               expect_sha256: str | None = None, expect_size: int | None = None) -> dict:
    groups, skipped = scan(chunk_dir)
    if not groups:
        raise TransferError(f"no RMSC1 chunks in {chunk_dir}")
    if transfer_id is None:
        if len(groups) > 1:
            raise TransferError(f"several transfers present, pass --transfer-id: {sorted(groups)}")
        transfer_id = next(iter(groups))
    if transfer_id not in groups:
        raise TransferError(f"transfer {transfer_id} not found; present: {sorted(groups)}")
    ordered = plan(groups[transfer_id])

    expect = expect_sha256.lower() if expect_sha256 else None
    if os.path.exists(out_file):
        if expect and sha256_file(out_file) == expect:
            return {"status": "already_present", "path": out_file, "sha256": expect}
        raise TransferError(f"{out_file} exists; refusing to overwrite it")

    os.makedirs(os.path.dirname(os.path.abspath(out_file)), exist_ok=True)
    part = out_file + ".part"
    h = hashlib.sha256()
    size = 0
    try:
        with open(part, "wb") as f:
            for c in ordered:
                body = c.payload()
                f.write(body)
                h.update(body)
                size += len(body)
            f.flush()
            os.fsync(f.fileno())
        digest = h.hexdigest()
        if expect_size is not None and size != expect_size:
            raise TransferError(f"rebuilt {size} bytes, expected {expect_size}")
        if expect and digest != expect:
            raise TransferError(f"whole-file SHA-256 {digest} does not match expected {expect}")
        os.replace(part, out_file)
    except BaseException:
        if os.path.exists(part):
            os.remove(part)
        raise
    os.chmod(out_file, 0o444)
    report = {"status": "rebuilt", "path": os.path.abspath(out_file), "bytes": size, "sha256": digest,
              "transfer_id": transfer_id, "chunks": len(ordered),
              "encodings": sorted({c.enc for c in ordered}), "skipped_files": skipped,
              "whole_file_hash_checked_against_uploader": bool(expect)}
    with open(out_file + ".transfer.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("chunk_dir")
    ap.add_argument("out_file")
    ap.add_argument("--transfer-id")
    ap.add_argument("--expect-sha256")
    ap.add_argument("--expect-size", type=int)
    a = ap.parse_args(argv)
    try:
        r = reassemble(a.chunk_dir, a.out_file, a.transfer_id, a.expect_sha256, a.expect_size)
    except TransferError as e:
        print(f"transfer failed: {e}", file=sys.stderr)
        return 1
    print(json.dumps(r, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
