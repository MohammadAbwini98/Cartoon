# Handoff: continue Animation Remake Studio locally (2026-11-04)

Read this first, then `STATUS.md`. Repository: https://github.com/MohammadAbwini98/Cartoon (public, branch `main`).

## Goal
Private, non-commercial research: remake episode 1 of «دروب ريمي» (`~/Downloads/دروب ريمي الحلقة 1 كاملة.mp4`)
with completely redesigned characters and environments, keeping the original narrative, shot timing,
acting and Arabic soundtrack. The MacBook Pro M3 Pro (18 GB) is the preferred GPU (MPS); cloud GPUs and paid
APIs are fallbacks that need an approved budget. Do not run paid services without one.

## What exists (all tested; see STATUS.md for evidence)
| Area | Where | State |
|---|---|---|
| Shot breakdown: frame-exact cuts, dissolves, fades, flashes, keyframes, audio, manifest | `remake_studio/preprocess/` | tested on synthetic video only |
| Browser uploader (claude.ai page, ≤ 1 GB) | `uploader/`, https://claude.ai/artifact/X4vNx9b2W1fCHL7dQryeH4 | live; episode NOT uploaded |
| Direct R2 uploads with presigned links | `remake_studio/storage.py`, `uploader/direct.html` | tested against an S3 stand-in, not R2 |
| Provider layer + budget gate (ComfyUI, RunPod, fal, Replicate) | `remake_studio/providers/` | mock-tested; no keys, no budget |
| Pilot ComfyUI graph (AnimateDiff + ControlNet lineart/pose + IPAdapter) and MPS variant | `remake_studio/workflows.py`, `workflows/node_schemas.json` | schema-checked from node source; never executed |
| Hybrid queue: signed jobs, Mac worker, scheduler, benchmark | `remake_studio/hybrid/` | tested with a ComfyUI stand-in; never run on the Mac |
| Mac one-command setup | `infra/mac-worker/setup.sh` → `remake_studio/hybrid/mac_setup.py` | tested on a simulated Mac only |
| Pinned versions and models (SHA-256) | `infra/comfyui/nodes.lock.json`, `infra/comfyui/models.json` | ComfyUI v0.43.2 + 5 node packs; ~8.8 GB models |
| Docs | `docs/` (STORAGE_SETUP, MAC_SETUP_CHECKLIST, HYBRID_RENDERING, MPS_COMPATIBILITY, QUALITY_ACCEPTANCE_MATRIX) | current |

Tests: `python3 -m unittest discover -s tests -t .` (97 pass), `node tests/transfer-core.test.mjs`,
`node tests/uploader_e2e.cjs` (needs Playwright). `python3 scripts/check_secrets.py .` must stay clean.

## Not done (blockers)
1. **Cloudflare R2**: create buckets `remake-queue`, `remake-media` and tokens `remake-cloud`, `remake-mac`
   (docs/STORAGE_SETUP.md); set the account id in `config/hybrid.toml`. Never commit keys.
2. **Mac worker**: from the repo on the Mac,
   `sh infra/mac-worker/setup.sh --queue-endpoint https://<account-id>.r2.cloudflarestorage.com --queue-bucket remake-queue`
   (add `--models-from ~/ComfyUI/models` or `--reuse-comfyui <path>` if ComfyUI already exists). It stops at
   the storage step until R2 exists. Diagnostics: `~/.remake-worker/diagnostics.json`.
3. **Episode**: not uploaded. Running locally, use the file directly; never commit media.
4. **Style checkpoint**: not chosen (benchmarks use the pinned SD 1.5 baseline).
5. **No real GPU run yet**: nothing is "compatible" until a real Mac run passes. The hybrid system is not operational.

## Suggested next steps on the Mac
1. `python3 -m remake_studio.preprocess.breakdown "<episode.mp4>" media/work/breakdown`: real shot analysis;
   check contact sheets and transition calibration; write `docs/EPISODE_SOURCE_ANALYSIS.md`.
2. Install ComfyUI per setup.sh (it can run without R2 up to the storage step), confirm MPS and loopback.
3. Run benchmark stages directly against local ComfyUI (B0 smoke 1 frame → B1 six frames → B2 48 frames →
   B3 96 frames); record peak memory, s/frame, failures (`remake_studio/hybrid/benchmark.py`).
4. Pick a short representative scene; build character/style bible; render the 2–4 s pilot with the
   original audio (`remake_studio/assemble.py`), compare side by side.

## Rules kept so far
Never overwrite the source MP4. Keep the original Arabic audio for the private test. No credentials, media,
weights or renders in Git. No paid API/GPU calls without an explicit budget. Report measured results only.
