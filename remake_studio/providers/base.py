"""Provider interface shared by self-hosted (ComfyUI) and paid API backends."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


TASKS = ("image", "image_to_image", "video", "video_to_video", "reference_to_video", "lipsync", "workflow")


@dataclass
class Capability:
    model: str                      # provider's model / endpoint id
    task: str                       # one of TASKS
    inputs: tuple = ()              # e.g. ("prompt", "image", "control_video", "reference_images")
    max_seconds: float | None = None
    notes: str = ""


@dataclass
class Cost:
    amount: float
    currency: str
    basis: str                      # how it was computed, with the price source
    price_checked_on: str = ""


@dataclass
class RemoteStatus:
    state: str                      # queued | running | succeeded | failed | canceled
    progress: float | None = None
    error: str | None = None
    outputs: list = field(default_factory=list)   # provider-specific output refs (urls, filenames)
    billed_seconds: float | None = None
    raw: dict = field(default_factory=dict)


class Provider(Protocol):
    name: str
    kind: str                       # "paid_api" | "self_hosted"

    def capabilities(self) -> list[Capability]: ...
    def estimate(self, model: str, request: dict) -> Cost | None: ...
    def submit(self, model: str, request: dict) -> str: ...
    def status(self, remote_id: str) -> RemoteStatus: ...
    def fetch(self, remote_id: str, status: RemoteStatus, dest_dir: str) -> list[str]: ...
    def cancel(self, remote_id: str) -> None: ...
