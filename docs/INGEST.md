# Getting files into the cloud workspace

## Uploader
Private page: https://claude.ai/artifact/X4vNx9b2W1fCHL7dQryeH4 (only the owner can open it).
Source: `uploader/index.html` + `uploader/transfer-core.js`. The repository is the source of truth; publish a
copy from the session scratchpad.

- Splits the file into 16 MiB pieces in the browser. Each piece carries an `RMSC1` header (transfer id,
  index, total, length, SHA-256) and is stored in the page's asset store as `video/mp4`. If the store
  refuses binary pieces, the page switches automatically to base64 pieces (`text/plain`, 14 MiB payload).
- The whole-file SHA-256 is computed in the browser while uploading, stored on the transfer record, and
  shown with the matching `shasum -a 256` command for an optional check on the Mac.
- Transfer records live in the page database: `transfers/<id>` (name, size, role, mode, status, sha256)
  and `transfers/<id>/chunks/<NNNNN>` (index, assetId, sha256, size).
- Resume: choose the same file again. Pieces whose stored SHA-256 matches are skipped. Pause stops after the
  current piece.
- Storage quota: 1 GiB and 5,000 files per page (measured 2026-11-02). A larger file needs a second page;
  the uploader warns before starting if the file will not fit.

## Pulling a transfer into the workspace (Claude runs this)
1. `ArtifactData list transfers` → find the record with `status: complete`; note `sha256`, `size`, `id`.
2. `ArtifactData list transfers/<id>/chunks` (limit 1000) → asset ids.
3. For each asset id: `Artifact read url=<uploader> path=<assetId> out_dir=media/incoming/<id>`
   (one call per piece; the `paths` batch form only covers published files, not assets).
4. `python3 -m remake_studio.ingest.reassemble media/incoming/<id> media/source/<name>
    --transfer-id <id> --expect-sha256 <sha256> --expect-size <size>`
   The output is written atomically, verified against the browser's hash and made read-only.

## Verified
- `tests/transfer-core.test.mjs`: JS SHA-256 vs node:crypto (vectors, 0–299 bytes, 40 MiB streamed).
- `tests/test_reassemble.py` (10 tests): bin and b64 pieces made by the real JS core, corruption, truncation,
  missing and duplicate pieces, mixed transfers, overwrite refusal, real 16 MiB pieces.
- `tests/uploader_e2e.cjs` (4 scenarios, headless Chromium, mocked capabilities): full upload + Python rebuild,
  failure then resume (no re-upload of stored pieces), binary refused → base64 fallback, outside claude.ai.
- Live round trip through the real asset store (2026-11-02): a 300 KB RMSC1 piece uploaded as `video/mp4`,
  downloaded with `Artifact read path=<assetId>`, rebuilt byte-identical (sha256 5c9ca567…f9f44b).
  Probe asset `cfd74b52783d9c8e985d0e9d0901786e` remains in the store (300 KB).

## Not yet verified
- A real browser upload of the episode through the claude.ai viewer (needs the user's file).
- Upload throughput and rate limits of the live asset store.
