# MPS compatibility (Apple Silicon)

Status 2026-11-03: **static audit done; no MPS benchmark has run yet.** Until the benchmark below records
evidence in `state/compat.json`, the scheduler does not send production renders to the Mac.

## Audit of the pilot graph's code paths (node source at the pinned commits)

| Node / pack | Finding | Consequence |
|---|---|---|
| ComfyUI core v0.43.2 | Detects MPS (`model_management.get_torch_device` → `mps`); disables non-blocking transfers on MPS | Supported device |
| AnimateDiff-Evolved `9257651` | No CUDA-only calls. xformers only if enabled (never on Mac). Explicit MPS branch for the groupnorm hack, which applies to v1/v2 motion models; we use v3 | Expected to run; memory unknown |
| Advanced-ControlNet `27a67fe` | Only `device="cuda"` hit is a comment | No CUDA dependency |
| IPAdapter_plus `a0f451a` | `torch.cuda.empty_cache()` ×4, which does not require a CUDA device | No CUDA dependency |
| controlnet_aux `a587a4c`, AnimeLineArt | No device-specific code | Expected to run |
| controlnet_aux, DWPose (torchscript) | Runs on `get_torch_device()`, i.e. MPS, never tested there by us | **Risk**: avoided in the MPS variant |
| controlnet_aux, DWPose (ONNX) | Default onnxruntime providers list has no CoreML; on a Mac it falls back to CPU | MPS variant uses the ONNX models; CoreML can be enabled with `AUX_ORT_PROVIDERS=CoreMLExecutionProvider,CPUExecutionProvider` and must be benchmarked separately |
| VideoHelperSuite `4d907be` | No CUDA-only paths in the loaders used | Expected to run |

The MPS variant (`workflows.variant_graph(..., "mps", ...)`) therefore differs from the CUDA one only in the
DWPose models (ONNX) and its default size (512×288, context window 16/4). No node is CUDA-only, but
"expected to run" is not evidence: memory, speed and output quality must be measured.

Launch ComfyUI on the Mac with `PYTORCH_ENABLE_MPS_FALLBACK=1` (unsupported ops fall back to CPU instead of
failing) and the default loopback listen address.

## Benchmark (never the full episode)

| Stage | Template | Frames | Measures | Gate to next stage |
|---|---|---|---|---|
| B0 | smoke_v1 | 1 | node loading, upload, output retrieval | succeeds |
| B1 | keyframes_v1 | 6 representative source frames | peak memory, s/frame, per-frame quality | succeeds within 14.4 GB (80 % of 18 GB) |
| B2 | pilot_shot_v1 | 48 (2 s) | + temporal metrics vs source | succeeds within limit |
| B3 | pilot_shot_v1 | 96 (4 s) | same | succeeds within limit |

Measured by the worker: peak system memory in use (ComfyUI `/system_stats`), peak ComfyUI process RSS,
wall time, s/frame, effective fps, failure kind (`oom`, `execution`, `comfyui_exited`). Measured in the cloud
from the frames: flicker ratio, hold instability, motion correlation against the source shot (proxies).
Quality and identity: side-by-side review against the CUDA candidate, recorded as `quality_equivalent`.

## Results

None yet. Requires: the Mac worker installed and authorized, ComfyUI with the pinned node packs and the
models from `infra/comfyui/models.json` on the Mac, and a source shot (the uploaded episode).
