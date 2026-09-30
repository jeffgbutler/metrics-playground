"""All configuration comes from environment variables (see .env.example at the repo root)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .util import parse_duration


def _bool(v: str | None, default: bool) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    base_rps: float = 30.0
    day_length: float = 3600.0
    seed: int | None = None
    advertise_host: str = "simulator"
    control_port: int = 8080
    restart_downtime: float = 5.0
    otlp_enabled: bool = True
    otlp_interval: float = 15.0
    honeycomb_config_key: str = ""
    honeycomb_api: str = "https://api.honeycomb.io"
    marker_dataset: str = "__all__"

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ.get
        seed = e("SIM_SEED")
        return cls(
            base_rps=float(e("SIM_BASE_RPS", "30")),
            day_length=parse_duration(e("SIM_DAY_LENGTH", "60m")),
            seed=int(seed) if seed else None,
            advertise_host=e("SIM_ADVERTISE_HOST", "simulator"),
            control_port=int(e("SIM_CONTROL_PORT", "8080")),
            restart_downtime=parse_duration(e("SIM_RESTART_DOWNTIME", "5s")),
            otlp_enabled=_bool(e("SIM_OTLP_ENABLED"), True),
            otlp_interval=parse_duration(e("SIM_OTLP_INTERVAL", "15s")),
            honeycomb_config_key=e("HONEYCOMB_CONFIG_KEY", ""),
            honeycomb_api=e("HONEYCOMB_API_ENDPOINT", "https://api.honeycomb.io").rstrip("/"),
            marker_dataset=e("HONEYCOMB_MARKER_DATASET", "__all__"),
        )
