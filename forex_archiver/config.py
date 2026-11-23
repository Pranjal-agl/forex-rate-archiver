"""Runtime settings. Defaults live here; override with env vars or a TOML file."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass
class Settings:
    # storage
    db_path: Path = Path("data/forex.db")
    csv_path: Path = Path("all_rates_history.csv")
    excel_dir: Path = Path("data")
    snapshot_dir: Path = Path("snapshots")
    dashboard_json: Path = Path("dashboard/data.json")

    # sanity band: anything outside is a parse error, not a market move
    min_plausible_rate: float = 60.0
    max_plausible_rate: float = 150.0

    # anomaly detection
    anomaly_method: str = "pct"           # "pct" | "mad" | "both"
    pct_threshold: float = 1.5            # flag |day-over-day change| above this many percent
    mad_threshold: float = 6.0            # flag robust z-score above this
    mad_window: int = 20                  # rolling window (observations) for median/MAD
    mad_floor_pct: float = 0.15           # MAD floor in percent of level; stops tiny-MAD blowups
    use_peers: bool = True                # score each bank's move net of the other banks' median move
    stale_days: int = 5                   # identical value this many observations in a row => stale

    # freshness / alerting
    max_staleness_days: int = 5           # alert if newest headline value is older than this

    # fetching
    fetch_attempts: int = 3
    request_timeout_s: float = 30.0

    @classmethod
    def load(cls, toml_path: str | Path | None = None) -> Settings:
        """Defaults <- TOML file (if any) <- FOREX_* environment variables."""
        s = cls()
        path = Path(toml_path or os.environ.get("FOREX_CONFIG", "forex_archiver.toml"))
        if path.exists():
            with path.open("rb") as f:
                for k, v in tomllib.load(f).items():
                    s._set(k, v)
        for f_ in fields(cls):
            env = os.environ.get(f"FOREX_{f_.name.upper()}")
            if env is not None:
                s._set(f_.name, env)
        return s

    def _set(self, name: str, value: object) -> None:
        current = getattr(self, name)  # AttributeError on unknown keys is intended
        if isinstance(current, Path):
            value = Path(str(value))
        elif isinstance(current, bool):
            value = str(value).lower() in {"1", "true", "yes"}
        elif isinstance(current, (int, float)):
            value = type(current)(value)
        setattr(self, name, value)
