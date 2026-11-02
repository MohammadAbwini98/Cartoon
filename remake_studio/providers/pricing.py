"""Price table: every price carries its source and the date it was checked.

config/pricing.toml
  [[price]]
  provider = "fal"
  model = "<model id>"
  unit = "per_video_second"      # per_video_second | per_image | per_gpu_second | per_job
  price = 0.0
  currency = "USD"
  source = "https://…"           # where the number was read
  checked_on = "2026-11-02"

No entry, no source, or a price older than `max_age_days` gives no estimate,
and the budget refuses a job without an estimate.
"""
from __future__ import annotations

import datetime as dt
import tomllib

from remake_studio.providers.base import Cost


class PriceTable:
    def __init__(self, entries: list[dict], max_age_days: int = 30, today=None):
        self.entries = entries
        self.max_age = max_age_days
        self.today = today or dt.date.today()

    @classmethod
    def load(cls, path: str, **kw) -> "PriceTable":
        try:
            with open(path, "rb") as f:
                return cls(tomllib.load(f).get("price", []), **kw)
        except FileNotFoundError:
            return cls([], **kw)

    def find(self, provider: str, model: str) -> dict | None:
        for e in self.entries:
            if e.get("provider") == provider and e.get("model") == model:
                return e
        return None

    def estimate(self, provider: str, model: str, request: dict) -> Cost | None:
        e = self.find(provider, model)
        if not e or e.get("price") is None or not e.get("source") or not e.get("checked_on"):
            return None
        checked = dt.date.fromisoformat(e["checked_on"])
        if (self.today - checked).days > self.max_age:
            return None
        unit, price = e["unit"], float(e["price"])
        qty = {"per_video_second": request.get("duration"),
               "per_image": request.get("num_images", 1),
               "per_gpu_second": request.get("est_gpu_seconds"),
               "per_job": 1}.get(unit)
        if qty is None:
            return None
        return Cost(round(price * float(qty), 6), e.get("currency", "USD"),
                    f"{price} {e.get('currency', 'USD')} {unit} × {qty} ({e['source']})", e["checked_on"])
