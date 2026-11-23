"""Shared fixtures. Bank snapshots in tests/fixtures/ were captured from a GitHub Actions runner on 2026-11-23."""

from __future__ import annotations

import gzip
from datetime import date
from pathlib import Path

import pytest

from forex_archiver.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"
RUN_DATE = date(2026, 11, 23)


def load(name: str) -> bytes:
    data = (FIXTURES / name).read_bytes()
    return gzip.decompress(data) if name.endswith(".gz") else data


SBI_HTML = "sbi_ny_2026-11-20.html"
HDFC_PDF = "hdfc_2026-11-23.pdf"
HDFC_STALE_PDF = "hdfc_stale_2025-01-13.pdf"
ICICI_HTML = "icici_2026-11-23.html.gz"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "forex.db",
        csv_path=tmp_path / "history.csv",
        excel_dir=tmp_path / "xlsx",
        snapshot_dir=tmp_path / "snapshots",
        dashboard_json=tmp_path / "dashboard.json",
    )


def fake_fetchers(routes: dict[str, bytes | Exception]) -> dict:
    """Fetchers that serve canned bytes per URL substring (or raise), and count calls."""
    calls: list[tuple[str, str]] = []

    def make(strategy: str):
        def fetch(url: str, timeout: float) -> bytes:
            calls.append((strategy, url))
            for key, val in routes.items():
                if key in url:
                    if isinstance(val, Exception):
                        raise val
                    return val
            from forex_archiver.errors import FetchError
            raise FetchError(f"no route for {url}")
        return fetch

    f = {"http": make("http"), "browser": make("browser")}
    f["calls"] = calls  # type: ignore[assignment]
    return f


GOOD_ROUTES = {
    "sbinewyork": load(SBI_HTML),
    "hdfc-bank-treasury-forex-card-rates": load(HDFC_PDF),
    "forex-rates/forex-rates.pdf": load(HDFC_STALE_PDF),
    "icici": load(ICICI_HTML),
}
