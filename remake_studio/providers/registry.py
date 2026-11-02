"""Build the configured backends from config/providers.toml and environment keys.

Keys are read only from the environment (cloud environment secrets), never
from files in the repository. A backend whose key is absent is reported as
"not configured" and is not constructed.
"""
from __future__ import annotations

import os
import tomllib

from remake_studio.providers.base import Capability
from remake_studio.providers.comfyui import ComfyUI
from remake_studio.providers.paid import FalQueue, Replicate, RunPodComfy
from remake_studio.providers.pricing import PriceTable


def _caps(entries) -> list[Capability]:
    return [Capability(e["model"], e["task"], tuple(e.get("inputs", ())), e.get("max_seconds"), e.get("notes", ""))
            for e in entries or []]


def build(config_path: str = "config/providers.toml", pricing_path: str = "config/pricing.toml",
          env=os.environ) -> tuple[dict, list[dict]]:
    try:
        with open(config_path, "rb") as f:
            cfg = tomllib.load(f)
    except FileNotFoundError:
        cfg = {}
    prices = PriceTable.load(pricing_path)
    providers, report = {}, []

    def need(section, *keys):
        missing = [env_name for env_name in (cfg.get(section, {}).get(k) for k in keys) if env_name and not env.get(env_name)]
        return missing

    if "comfyui" in cfg:
        c = cfg["comfyui"]
        miss = need("comfyui", "url_env")
        if miss:
            report.append({"provider": "comfyui", "status": "not configured", "missing_env": miss})
        else:
            providers["comfyui"] = ComfyUI(env[c["url_env"]], env.get(c.get("token_env", ""), None) or None,
                                           gpu_rate_per_hour=c.get("gpu_rate_per_hour"),
                                           currency=c.get("currency", "USD"), rate_source=c.get("rate_source", ""))
            report.append({"provider": "comfyui", "status": "configured", "billable": providers["comfyui"].billable})
    if "runpod" in cfg:
        c = cfg["runpod"]
        miss = need("runpod", "endpoint_env", "key_env")
        if miss:
            report.append({"provider": "runpod", "status": "not configured", "missing_env": miss})
        else:
            providers["runpod"] = RunPodComfy(env[c["endpoint_env"]], env[c["key_env"]], prices)
            report.append({"provider": "runpod", "status": "configured", "billable": True})
    for name, cls, keyname in (("fal", FalQueue, "key_env"), ("replicate", Replicate, "key_env")):
        if name in cfg:
            c = cfg[name]
            miss = need(name, keyname)
            if miss:
                report.append({"provider": name, "status": "not configured", "missing_env": miss})
            else:
                providers[name] = cls(env[c[keyname]], prices, _caps(c.get("models")))
                report.append({"provider": name, "status": "configured", "billable": True})
    return providers, report
