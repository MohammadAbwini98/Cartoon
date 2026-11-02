"""ComfyUI API-format graphs for the remake pilot.

pilot_shot_graph: one source shot → redesigned frames, conditioned on
  * structure: anime line art of every source frame (ControlNet lineart_anime)
  * acting: DWPose body/hand/face pose of every source frame (ControlNet openpose)
  * identity: the character bible reference image (IPAdapter)
  * motion: AnimateDiff motion module with sliding context windows
and generated from empty latents (denoise 1.0), so no source pixels or colours
are carried over: the source drives structure and timing only.

Frames are written with SaveImage (PNG sequence). The pipeline encodes them at
the source frame rate and muxes the original audio itself, because the RunPod
ComfyUI worker (5.11.0) returns only image outputs, and because frame-exact
assembly belongs in our own code.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

SCHEMAS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workflows", "node_schemas.json")


@dataclass
class PilotParams:
    control_video: str            # file name in ComfyUI's input dir (the source shot, uploaded)
    reference_image: str          # character bible reference, uploaded
    frames: int                   # exact source frame count of the shot
    width: int = 768              # generation size (multiple of 8); upscaled later
    height: int = 432
    checkpoint: str = "CHECKPOINT_TBD.safetensors"
    motion_module: str = "v3_sd15_mm.ckpt"
    lineart_cnet: str = "control_v11p_sd15s2_lineart_anime.pth"
    pose_cnet: str = "control_v11p_sd15_openpose.pth"
    positive: str = ""
    negative: str = "photo, realistic, 3d render, blurry, deformed hands, extra fingers, text, watermark"
    seed: int = 1
    steps: int = 24
    cfg: float = 7.0
    sampler: str = "euler_ancestral"
    scheduler: str = "normal"
    lineart_strength: float = 0.8
    pose_strength: float = 0.7
    ipadapter_preset: str = "PLUS (high strength)"
    ipadapter_weight: float = 0.8
    context_length: int = 16
    context_overlap: int = 4
    output_prefix: str = "remake/frame"
    extra: dict = field(default_factory=dict)


def pilot_shot_graph(p: PilotParams) -> dict:
    if p.width % 8 or p.height % 8:
        raise ValueError("width and height must be multiples of 8")
    g = {
        "1": ("CheckpointLoaderSimple", {"ckpt_name": p.checkpoint}),
        "2": ("VHS_LoadVideo", {"video": p.control_video, "force_rate": 0, "custom_width": p.width,
                                "custom_height": p.height, "frame_load_cap": p.frames, "skip_first_frames": 0,
                                "select_every_nth": 1}),
        "3": ("AnimeLineArtPreprocessor", {"image": ["2", 0], "resolution": min(p.width, p.height)}),
        "4": ("DWPreprocessor", {"image": ["2", 0], "detect_hand": "enable", "detect_body": "enable",
                                 "detect_face": "enable", "resolution": min(p.width, p.height),
                                 "bbox_detector": "yolox_l.onnx", "pose_estimator": "dw-ll_ucoco_384_bs5.torchscript.pt",
                                 "scale_stick_for_xinsr_cn": "disable"}),
        "5": ("LoadImage", {"image": p.reference_image}),
        "6": ("IPAdapterUnifiedLoader", {"model": ["1", 0], "preset": p.ipadapter_preset}),
        "7": ("IPAdapterAdvanced", {"model": ["6", 0], "ipadapter": ["6", 1], "image": ["5", 0],
                                    "weight": p.ipadapter_weight, "weight_type": "linear", "combine_embeds": "concat",
                                    "start_at": 0.0, "end_at": 1.0, "embeds_scaling": "V only"}),
        "8": ("ADE_StandardStaticContextOptions", {"context_length": p.context_length,
                                                   "context_overlap": p.context_overlap}),
        "9": ("ADE_AnimateDiffLoaderGen1", {"model": ["7", 0], "model_name": p.motion_module,
                                            "beta_schedule": "autoselect", "context_options": ["8", 0]}),
        "10": ("CLIPTextEncode", {"text": p.positive, "clip": ["1", 1]}),
        "11": ("CLIPTextEncode", {"text": p.negative, "clip": ["1", 1]}),
        "12": ("ACN_ControlNetLoaderAdvanced", {"cnet": p.lineart_cnet}),
        "13": ("ACN_ControlNetLoaderAdvanced", {"cnet": p.pose_cnet}),
        "14": ("ACN_AdvancedControlNetApply_v2", {"positive": ["10", 0], "negative": ["11", 0], "control_net": ["12", 0],
                                                  "image": ["3", 0], "strength": p.lineart_strength,
                                                  "start_percent": 0.0, "end_percent": 1.0}),
        "15": ("ACN_AdvancedControlNetApply_v2", {"positive": ["14", 0], "negative": ["14", 1], "control_net": ["13", 0],
                                                  "image": ["4", 0], "strength": p.pose_strength,
                                                  "start_percent": 0.0, "end_percent": 1.0}),
        "16": ("EmptyLatentImage", {"width": p.width, "height": p.height, "batch_size": p.frames}),
        "17": ("KSampler", {"model": ["9", 0], "seed": p.seed, "steps": p.steps, "cfg": p.cfg,
                            "sampler_name": p.sampler, "scheduler": p.scheduler, "positive": ["15", 0],
                            "negative": ["15", 1], "latent_image": ["16", 0], "denoise": 1.0}),
        "18": ("VAEDecode", {"samples": ["17", 0], "vae": ["1", 2]}),
        "19": ("SaveImage", {"images": ["18", 0], "filename_prefix": p.output_prefix}),
    }
    return {k: {"class_type": c, "inputs": i} for k, (c, i) in g.items()}


def check_graph(graph: dict, schemas_path: str = SCHEMAS) -> list[str]:
    """Static check against node_schemas.json: known node types, required
    inputs present, no unknown inputs, links point at existing nodes and
    valid output slots, and listed choices respected."""
    with open(schemas_path, encoding="utf-8") as f:
        schemas = json.load(f)
    problems = []
    for nid, node in graph.items():
        s = schemas.get(node["class_type"])
        if not s:
            problems.append(f"{nid}: unknown node type {node['class_type']}")
            continue
        inputs = node["inputs"]
        for r in s["required"]:
            if r not in inputs:
                problems.append(f"{nid} {node['class_type']}: missing required input {r!r}")
        for k, v in inputs.items():
            if k not in s["required"] and k not in s["optional"]:
                problems.append(f"{nid} {node['class_type']}: unknown input {k!r}")
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                src = graph.get(v[0])
                if src is None:
                    problems.append(f"{nid}.{k}: links to missing node {v[0]}")
                elif v[1] >= schemas.get(src["class_type"], {}).get("outputs", 0):
                    problems.append(f"{nid}.{k}: output {v[1]} of {src['class_type']} does not exist")
            elif k in s.get("choices", {}) and v not in s["choices"][k]:
                problems.append(f"{nid}.{k}: {v!r} not one of {s['choices'][k]}")
    return problems
