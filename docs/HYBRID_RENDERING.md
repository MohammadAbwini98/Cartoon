# Hybrid rendering

The cloud session orchestrates everything; the MacBook Pro M3 Pro (18 GB) renders when it is online and the
workflow has passed the MPS benchmark. Cloud GPUs and APIs are fallbacks behind the same job interface.

```
cloud orchestrator ── signed job ──▶ private bucket (S3-compatible) ◀── polls (outbound only) ── Mac worker
   │  scheduler, JobStore, budget         jobs/ claims/ status/ cancel/        │ verifies signature, template,
   │  dashboard snapshot                  inputs/ results/ workers/            │ nodes, inputs; drives the
   ▼                                                                           ▼ loopback ComfyUI (MPS)
 cloud GPU (RunPod/ComfyUI) · APIs (fal/Replicate) · container CPU     results + metrics + heartbeat
```

## Backends and routing (`remake_studio/hybrid/scheduler.py`)

| Task | Backend |
|---|---|
| preprocess, encode, assemble, analysis | cloud container CPU |
| ComfyUI template validated on MPS, within the frames/size/memory it was validated at, quality confirmed | **Mac** (cost 0, time from measured s/frame) |
| Mac offline, task allows fallback, cloud GPU inside an authorized budget | cheapest authorized cloud GPU |
| Mac offline, no fallback or no budget | waits in the queue for the Mac; state preserved |
| not validated on MPS | cloud GPU if authorized, otherwise blocked with the reason |
| benchmark jobs | Mac only (they produce the MPS evidence) |

`remake hybrid decide TEMPLATE FRAMES WxH [--benchmark] [--fallback]` prints the choice and why each
alternative was rejected.

## Security

* The worker opens no port. It makes outbound HTTPS requests to the bucket and HTTP requests to ComfyUI on
  loopback only (it refuses any other ComfyUI address). No desktop control, no shell, no file access beyond
  its own state folder and temporary files.
* A job runs only if its HMAC-SHA256 signature verifies with the key created on the Mac at install time,
  its template is on the worker's allowlist, every node belongs to that template's node set, every loaded
  file is one of its own hash-verified inputs, and outputs go under `remake/<job_id>/`.
* Key, config and storage credentials live in `~/.remake-worker/` with mode 600; the worker refuses to start
  if they are readable by others. The bucket token should be scoped to the one bucket.
* No administrator rights: user LaunchAgent, user files; `caffeinate -i` only while a job runs.

## Availability and recovery

* Heartbeat every poll (`workers/<id>.json`): time, hardware, ComfyUI version and device, busy job.
  Offline = no heartbeat for 120 s.
* Claims are leases (180 s, renewed while running). A sleeping or crashed Mac's job becomes reclaimable.
* Worker restart: the in-flight ComfyUI prompt id is kept on disk; the worker collects that prompt instead of
  rendering again, and resubmits once only if ComfyUI lost it.
* Cancel: the orchestrator writes `cancel/<job_id>`; the worker interrupts the prompt.
* Jobs and their state live in the cloud JobStore and the bucket, so nothing depends on the Mac being online.

## Setting up the Mac (owner's action)

1. Clone the project on the Mac and run `sh infra/mac-worker/install.sh` (no sudo). It creates
   `~/.remake-worker/` with a new job-signing key, config and an empty secrets file, and a LaunchAgent.
2. Create a private bucket (Cloudflare R2 suggested) and one API token limited to that bucket. Put its keys in
   `~/.remake-worker/secrets.json` and the endpoint/bucket in `~/.remake-worker/config.json`.
3. In ComfyUI on the Mac: install the five node packs at the commits in `infra/comfyui/nodes.lock.json`,
   fetch models with `python3 infra/comfyui/fetch_models.py <ComfyUI>/models`, start it with
   `PYTORCH_ENABLE_MPS_FALLBACK=1` (default listen 127.0.0.1).
4. `python3 -m remake_studio.hybrid.worker --selftest` checks the bucket (including conditional writes) and ComfyUI.
5. In the cloud environment settings add secrets `REMAKE_STORE_ACCESS_KEY`, `REMAKE_STORE_SECRET_KEY`
   (a separate token for the same bucket) and `REMAKE_JOB_SIGNING_KEY` (the contents of the Mac's
   `job-signing.key`), and set the endpoint and bucket in `config/hybrid.toml`.
6. `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.remake-studio.worker.plist`.
