# Status — 2026-11-02

Milestone being built toward: a cloud system that receives the episode MP4, analyses it, generates a
redesigned short scene, keeps the original Arabic audio and returns a downloadable result.

## Verified (executed, with results)

| Component | Evidence |
|---|---|
| Browser uploader (`uploader/`), published privately at https://claude.ai/artifact/X4vNx9b2W1fCHL7dQryeH4 | Headless Chromium against mocked claude.ai capabilities: 3-piece upload with page hash = node hash and Python rebuild; failure on piece 2 then resume with no re-upload; binary refused → base64 fallback; outside claude.ai stays inert (`tests/uploader_e2e.cjs`, 4/4) |
| Live asset-store round trip | 300 KB RMSC1 piece uploaded as `video/mp4`, downloaded by asset id, rebuilt byte-identical (sha256 `5c9ca567…f9f44b`). Store quota measured: 1 GiB, 5,000 files per page |
| Browser SHA-256 | Matches node:crypto on FIPS vectors, all lengths 0–299 bytes, 40 MiB streamed in uneven pieces (~96 MiB/s in Node) |
| Reassembly (`ingest/reassemble.py`) | 10 tests: bin and b64 pieces from the real JS core, corruption, truncation, missing and duplicate pieces, mixed transfers, overwrite refusal, 16 MiB pieces |
| Shot breakdown (`preprocess/breakdown.py`) | Synthetic 23.976 fps episodes (near-lossless and crf 28): hard cuts exact, 3-frame shot kept, frame count exact, keyframes exact (18/18 by PSNR in v2 validation), black/freeze/letterbox/interlace detected |
| Dissolve, fade and flash detection (`preprocess/transitions.py`) | 9 scenarios + 2 episode tests: static, moving and 36-frame dissolves and fade out/in found within ±2 frames with correct kind; slow pan, zoom, moving object and in-place colour cycling not reported; flash returning to the same shot removes both cuts; flash into a new shot keeps them. Previously missed cross-dissolve is now detected |
| Provider layer (`providers/`) | 19 tests against scripted HTTP servers: ComfyUI discovery/upload/prompt/history/view, execution error, missing nodes refused before submit; fal, Replicate, RunPod request/response handling; no authorization or unknown price → nothing sent; per-job and total caps incl. open reservations; unknown submit outcome never resubmitted; secret redaction; idempotent job ids |
| Pilot graph (`workflows.py`) | Generated graph passes a static check against node schemas read from source at the pinned commits; checker catches renamed nodes, bad choices, missing inputs, bad output slots |
| Shot assembly (`assemble.py`) | 48 generated frames → 23.976 fps MP4, frame-exact, upscaled to 512×288, original audio aligned within 12 ms; wrong frame count refused; side-by-side evidence video |
| GPU worker pins (`infra/comfyui/`) | Dockerfile, lock file and node schemas reference identical commits; all model URLs pin a Hugging Face commit with size and SHA-256 from the HF API; fetcher verifies and refuses corrupt or unpinned files |

## Implemented, not yet tested for real

- **Paid adapters against live services** (RunPod, fal, Replicate): built from documented request shapes
  and tested only against mock servers.
- **GPU worker image**: `infra/comfyui/Dockerfile` has not been built or run. Its base tag
  `runpod/worker-comfyui:5.11.0-base` follows the documented tag format but has not been pulled.
- **Pilot graph execution**: never run in ComfyUI. Installing and running ComfyUI in this session was
  blocked by the session's code-execution policy, so the graph is checked only against node source.
- **Breakdown and detectors on the real episode**: thresholds are calibrated on synthetic clips only.
- **Uploader in the real claude.ai viewer with a large file**: the logic is tested with mocks plus one live
  300 KB round trip; throughput and rate limits on a multi-hundred-MB upload are unmeasured.

## Blockers

| Blocker | What clears it |
|---|---|
| Episode not uploaded | Owner uploads it with the uploader page |
| No GPU in this container | A cloud GPU backend (RunPod serverless or a GPU pod running ComfyUI) with an API key and a budget |
| No persistent repository | Owner creates an empty private GitHub repo; this session attaches it and pushes |
| Base checkpoint not chosen | Chosen at gate G1 with the style bible; licence and SHA-256 recorded in `infra/comfyui/models.json` |
| Character extraction, bible, lip-sync evaluation | Need the real episode (and a GPU for generation) |

## External authorizations needed (nothing billable has been called)

1. **GPU compute**: provider choice (RunPod recommended: the worker image is built for it) plus API key as an
   environment secret (`RUNPOD_API_KEY`, `RUNPOD_ENDPOINT_ID`, or `COMFYUI_URL` for a pod).
2. **Network volume** for ~7.2 GB of pinned models plus the checkpoint (storage is billed by the provider).
3. **Optional premium video model** (fal or Replicate) for the G2 comparison: key as a secret plus prices with
   sources entered in `config/pricing.toml`.
4. **Spending authorization**: total cap, per-job cap, providers, scope and expiry, recorded in
   `state/authorization.json` from your message. Without it, every paid job stops at `blocked`.

## Next execution step

Upload `دروب ريمي الحلقة 1 كاملة.mp4` with the uploader page. Then, in the cloud: pull the pieces
(`docs/INGEST.md`), rebuild and verify the hash, run the breakdown on the real episode, review the contact
sheets and transition calibration, and write `EPISODE_SOURCE_ANALYSIS.md` with measured results.
