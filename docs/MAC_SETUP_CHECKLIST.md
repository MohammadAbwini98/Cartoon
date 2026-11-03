# Mac setup and diagnostic checklist

## Run
The repository is private, so the Mac needs your GitHub sign-in once. Either sign in with the GitHub CLI
(`gh auth login`, then `gh repo clone MohammadAbwini98/Cartoon ~/RemakeStudio/Cartoon`), or clone over HTTPS and
let macOS Keychain store the credential git asks for:
```bash
git clone https://github.com/MohammadAbwini98/Cartoon.git ~/RemakeStudio/Cartoon
```
```bash
cd ~/RemakeStudio/Cartoon
```
```bash
sh infra/mac-worker/setup.sh --queue-endpoint https://<account-id>.r2.cloudflarestorage.com --queue-bucket remake-queue
```
Already have ComfyUI? Add `--models-from ~/ComfyUI/models` to reuse its verified model files in place, or
`--reuse-comfyui ~/ComfyUI` to use that install (it is checked, never upgraded). Setup asks for the
`remake-mac` token (hidden input). No sudo; nothing outside your home folder changes.

## What setup checks (all recorded in `~/.remake-worker/diagnostics.json` and sent to the queue bucket)

| # | Check | Pass means |
|---|---|---|
| 1 | detect | macOS on Apple Silicon; chip, memory (expect ~18 GB), macOS 14+, ≥ 40 GB free, git present |
| 2 | python | Python 3.10–3.13 (uv installs 3.12 in your home folder if needed) |
| 3 | comfyui | ComfyUI v0.43.2 at commit 423cbd2 (dedicated), or your install noted as-is |
| 4 | nodes | 5 node packs at the pinned commits (mismatches in a reused install are reported, not changed) |
| 5 | models | every pinned model verified by SHA-256; existing files reused in place; about 8.8 GB new otherwise |
| 6 | comfy_svc | ComfyUI LaunchAgent: `--listen 127.0.0.1`, `PYTORCH_ENABLE_MPS_FALLBACK=1` |
| 7 | loopback | ComfyUI answers on 127.0.0.1; `lsof` shows only loopback; the LAN address is refused |
| 8 | mps | PyTorch reports MPS and runs a matmul on it; ComfyUI reports device `mps`; all pilot nodes loaded |
| 9 | worker_cfg | `~/.remake-worker` mode 700, key/secrets/config mode 600, production disabled |
| 10 | storage | queue bucket read/write/list/delete + conditional writes; heartbeat written |
| 11 | worker_svc | worker LaunchAgent loaded; accepts signed benchmark jobs only |

## After setup (Claude, from the cloud)
`remake hybrid status` should show the Mac online with its hardware and ComfyUI version.
`remake hybrid bench submit` queues stage B0 (one frame); B1–B3 need the uploaded episode's source shot.
