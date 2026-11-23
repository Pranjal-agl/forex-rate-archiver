"""Pandas cleaning and dedup helpers, used for the legacy CSV and for exports."""

from __future__ import annotations

import logging

import pandas as pd

from .config import Settings

logger = logging.getLogger(__name__)

LEGACY_COLUMNS = {"Bank": "bank", "Slab_Type": "label", "Rate": "rate", "Date": "rate_date"}


def normalise(df: pd.DataFrame, settings: Settings) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Coerce types, normalise labels, and split into (clean, rejected) frames.

    Rejected rows keep a ``reason`` column. Nothing is silently dropped.
    """
    df = df.rename(columns=LEGACY_COLUMNS).copy()
    df["bank"] = df["bank"].astype(str).str.strip().str.upper()
    df["label"] = df["label"].astype(str).str.replace(r"\s+", " ", regex=True).str.strip()
    df["rate"] = pd.to_numeric(df["rate"], errors="coerce")
    df["rate_date"] = pd.to_datetime(df["rate_date"], errors="coerce").dt.date

    reason = pd.Series("", index=df.index)
    reason[df["rate"].isna()] = "non-numeric rate"
    reason[df["rate_date"].isna()] = "unparseable date"
    out_of_band = df["rate"].notna() & ~df["rate"].between(settings.min_plausible_rate, settings.max_plausible_rate)
    reason[out_of_band] = "outside plausible band"
    rejected = df[reason != ""].assign(reason=reason[reason != ""])
    if len(rejected):
        logger.warning("rejected %d row(s): %s", len(rejected), rejected["reason"].value_counts().to_dict())
    return dedupe(df[reason == ""]), rejected


def dedupe(df: pd.DataFrame) -> pd.DataFrame:
    """Last write wins per (rate_date, bank, label), mirroring the SQL upsert."""
    return df.drop_duplicates(subset=["rate_date", "bank", "label"], keep="last").reset_index(drop=True)
