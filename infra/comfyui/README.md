# GPU worker

Two ways to run the pilot graph on a cloud GPU; both use the same pinned node packs and models.

**A. RunPod serverless (recommended).** Build `Dockerfile` (base `runpod/worker-comfyui:5.11.0-base` plus the
five node packs at the commits in `nodes.lock.json`), create a serverless endpoint from it, and attach a network
volume. Fill the volume once with `python3 fetch_models.py /runpod-volume/models`. Set the environment secrets
`RUNPOD_API_KEY` and `RUNPOD_ENDPOINT_ID`. Pay per second of GPU while jobs run.

**B. GPU pod running ComfyUI.** Run the same image (or ComfyUI v0.43.2 plus the same node packs) on a pod,
expose port 8188 behind a token, and set `COMFYUI_URL` (and `COMFYUI_TOKEN`). Set `gpu_rate_per_hour` in
`config/providers.toml` so GPU time is metered against the budget.

The RunPod worker returns image outputs only. The pilot graph therefore ends in `SaveImage`, and
`remake_studio/assemble.py` encodes the frames with the original audio.

First contact with any GPU: run a small smoke job before the pilot (a 16-frame graph at 512×288), to confirm
node loading, model paths and output retrieval. Nothing here has been built or run yet; see `STATUS.md`.
