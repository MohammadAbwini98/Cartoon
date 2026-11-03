# Animation Remake Studio

Cloud pipeline that rebuilds an animated episode's visuals (new character and environment designs) while
keeping its narrative, shot timing, acting and original Arabic soundtrack. Private, non-commercial research.

Repository: https://github.com/MohammadAbwini98/Cartoon.
Current state, verified results and next steps: **[STATUS.md](STATUS.md)**.
Mac render worker: **[docs/MAC_SETUP_CHECKLIST.md](docs/MAC_SETUP_CHECKLIST.md)**.

## Layout

| Path | What it is |
|---|---|
| `uploader/` | Browser uploader (published privately on claude.ai) sending files in checked, resumable 16 MiB pieces |
| `remake_studio/ingest/reassemble.py` | Rebuilds an uploaded file from its pieces, verifying every hash |
| `remake_studio/preprocess/` | Frame-exact shot breakdown: cuts, dissolves, fades, flashes, keyframes, contact sheets, audio, manifest |
| `remake_studio/providers/` | Generation backends (ComfyUI, RunPod, fal, Replicate) behind a budget gate and job runner |
| `remake_studio/workflows.py`, `workflows/` | Pilot ComfyUI graph and the node schemas it is checked against |
| `remake_studio/assemble.py` | Encodes generated frames at the source frame rate with the original audio; side-by-side evidence |
| `infra/comfyui/` | GPU worker image, pinned node packs, model manifest with SHA-256, model fetcher |
| `config/` | Backend config (keys come from environment secrets), sourced price table, authorization template |
| `docs/` | Resource assessment, acceptance matrix, ingest runbook, validation records |
| `tests/` | Python, Node and headless-browser tests |

## Commands

```bash
python3 -m remake_studio.cli breakdown "<episode.mp4>" media/work/breakdown
python3 -m remake_studio.cli ingest media/incoming/<transfer> media/source/<name> --expect-sha256 <hex>
python3 -m remake_studio.cli providers
python3 -m remake_studio.cli budget
```

```bash
python3 -m unittest discover -s tests -t .
node tests/transfer-core.test.mjs
node tests/uploader_e2e.cjs
```

Media (`media/`), run state (`state/`), keys and model weights are never committed.
