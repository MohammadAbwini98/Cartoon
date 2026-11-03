# Status — 2026-11-03 (deployment pass)

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

## Deployment pass (commit after 3846e05)

**Hybrid system: NOT operational.** It has not processed a real job on the Mac or on a cloud GPU.

| Area | State | Evidence |
|---|---|---|
| GitHub | **blocked** | This session cannot create repositories (API: "sessions are bound to their configured repositories"). Needs an empty private repo created by the owner. |
| Security model | verified against the stand-in | signed-but-malicious jobs (expired, wrong worker, path traversal, absolute path, extra input, oversize batch/side, bad input name) never reach ComfyUI; replay after status deletion refused; production jobs wait; input URLs only from approved HTTPS hosts; forged result manifests refused; secrets absent from logs and store; tracked-file secret scan clean |
| Storage (R2) | implemented, **not verified on R2** | presigned signer matches the AWS example; real Chromium uploaded a 13 MB file in 3 parts directly to a SigV4-enforcing stand-in, cloud completed and verified SHA-256; expired and redirected links refused; wrong secret refused. No R2 credentials exist in this environment. |
| Mac setup | implemented, **never run on a Mac** | full flow on a simulated Mac with real files, permissions, hashing and the queue stand-in: fresh install, model reuse without download, LAN exposure and missing MPS fail loudly, reuse mode never changes an existing install |
| Benchmark | ready, **no results** | CLI submit → worker → collect → evidence recorded (B0 path); B1–B3 need the source shot |
| First Remi remake | **blocked** | episode not uploaded |

Running third-party server code here (ComfyUI, MinIO) is blocked by this session's execution policy, so
no real ComfyUI or real S3 server ran in this environment.

## Hybrid rendering (Mac M3 Pro + cloud) — built and tested here; **not yet operational**

Not operational until: the worker is installed on the Mac, the shared bucket and keys exist, and the MPS
benchmark (docs/MPS_COMPATIBILITY.md) has produced evidence. No Mac or GPU has run any of it yet.

| Component | Verified here |
|---|---|
| Worker (`hybrid/worker.py`) as a real process against a ComfyUI stand-in process | signed job end to end through the common Runner; memory sampled (process RSS + `/system_stats`), s/frame, effective fps; tampered signature and non-template node rejected before reaching ComfyUI; cancel interrupts the running prompt; worker killed mid-job and restarted collects the same prompt (1 render); ComfyUI killed and restarted with empty history → one resubmit, success; "MPS backend out of memory" classified `oom`; live lease respected, expired lease recovered; public ComfyUI URL and world-readable key refused |
| Offline Mac | job created while no worker runs stays queued with state persisted; completes when the worker appears |
| Transport | SigV4 signer reproduces the AWS documentation signature; S3 transport passes its self-test (incl. conditional writes) against a bucket stand-in; local store self-test |
| Scheduler | 8 routing tests: CPU tasks; untested MPS never assumed; benchmark Mac-only and queued when offline; validated → Mac; offline → wait or authorized fallback; frame/size/memory limits; quality confirmation required |
| Benchmark | staged plan (1 → 6 → 48 → 96 frames), temporal metrics, evidence recording (only success within 14.4 GB validates) |
| MPS workflow variants | pass node schemas and template allowlists; DWPose on ONNX |
| Mac installer | dry run in a scratch home: files created mode 600, key kept on rerun, LaunchAgent plist parses |
| Production view | new section on the studio page renders workers, jobs, MPS evidence, budget (browser test); first snapshot published |

Not verified: anything on macOS (MPS, `caffeinate`, `sysctl`, `launchctl`), a real ComfyUI, a real bucket,
and every benchmark number. The memory figures in tests come from the stand-in process.

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

## Owner actions for hybrid rendering
1. Create a private S3-compatible bucket (Cloudflare R2 suggested) and two tokens limited to it (Mac, cloud).
2. On the Mac: `sh infra/mac-worker/install.sh`, fill `~/.remake-worker/secrets.json`, set up ComfyUI with the
   pinned node packs and models, run `--selftest`, then load the LaunchAgent (docs/HYBRID_RENDERING.md).
3. Cloud environment secrets: `REMAKE_STORE_ACCESS_KEY`, `REMAKE_STORE_SECRET_KEY`, `REMAKE_JOB_SIGNING_KEY`.

## Next execution step

Upload `دروب ريمي الحلقة 1 كاملة.mp4` with the uploader page. Then, in the cloud: pull the pieces
(`docs/INGEST.md`), rebuild and verify the hash, run the breakdown on the real episode, review the contact
sheets and transition calibration, and write `EPISODE_SOURCE_ANALYSIS.md` with measured results.
