"""SQLite persistence: idempotent upserts, run/health logging, snapshot index."""

from __future__ import annotations

import gzip
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

import pandas as pd

from .models import RateRecord, RawSnapshot, SourceResult


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class UpsertStats:
    inserted: int = 0
    revised: int = 0
    unchanged: int = 0


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(resources.files("forex_archiver").joinpath("schema.sql").read_text())

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── runs ──────────────────────────────────────────────────────────────
    def start_run(self, trigger: str = "manual", git_sha: str | None = None) -> int:
        with self.conn:
            cur = self.conn.execute("INSERT INTO runs(started_at, trigger, git_sha) VALUES (?,?,?)",
                                    (_now(), trigger, git_sha))
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE runs SET finished_at=?, status=? WHERE run_id=?", (_now(), status, run_id))

    # ── raw layer ─────────────────────────────────────────────────────────
    def save_snapshot(self, snap: RawSnapshot, run_id: int, root: Path) -> str:
        """Store gzipped bytes under root/YYYY/MM/DD/ once per unique content hash."""
        rel = Path(snap.fetched_at.strftime("%Y/%m/%d")) / f"{snap.source}-{snap.sha256[:12]}.{snap.kind}.gz"
        dest = root / rel
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(dest, "wb") as f:
                f.write(snap.content)
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO raw_snapshots(sha256, source, url, kind, size_bytes, first_run, fetched_at, path)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (snap.sha256, snap.source, snap.url, snap.kind, len(snap.content), run_id,
                 snap.fetched_at.isoformat(timespec="seconds"), str(rel)),
            )
        return snap.sha256

    def record_health(self, run_id: int, res: SourceResult, snapshot_sha: str | None) -> None:
        as_of = max((r.rate_date for r in res.records), default=None)
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO source_health VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run_id, res.source, int(res.ok), round(res.latency_s, 3), res.attempts, len(res.records),
                 as_of.isoformat() if as_of else None, res.error_type, res.error, snapshot_sha),
            )

    # ── cleaned layer ─────────────────────────────────────────────────────
    UPSERT = """
        INSERT INTO rates (rate_date, bank, label, rate, source, snapshot, first_seen_at, last_seen_at)
        VALUES (:rate_date, :bank, :label, :rate, :source, :snapshot, :now, :now)
        ON CONFLICT (rate_date, bank, label) DO UPDATE SET
            revisions    = rates.revisions + (excluded.rate <> rates.rate),
            rate         = excluded.rate,
            source       = excluded.source,
            snapshot     = COALESCE(excluded.snapshot, rates.snapshot),
            last_seen_at = excluded.last_seen_at
    """

    def upsert_rates(self, records: Iterable[RateRecord], *, source: str, snapshot: str | None = None) -> UpsertStats:
        """Idempotent write keyed on (rate_date, bank, label). Re-running a day changes nothing."""
        stats = UpsertStats()
        now = _now()
        with self.conn:
            for r in records:
                key = (r.rate_date.isoformat(), r.bank, r.label)
                prev = self.conn.execute(
                    "SELECT rate FROM rates WHERE rate_date=? AND bank=? AND label=?", key).fetchone()
                if prev is None:
                    stats.inserted += 1
                elif prev["rate"] != r.rate:
                    stats.revised += 1
                else:
                    stats.unchanged += 1
                self.conn.execute(self.UPSERT, {"rate_date": key[0], "bank": r.bank, "label": r.label,
                                                "rate": r.rate, "source": source, "snapshot": snapshot, "now": now})
        return stats

    def set_headline(self, bank: str, label: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO headline_labels VALUES (?,?)", (bank, label))

    # ── quality layer ─────────────────────────────────────────────────────
    def replace_anomalies(self, flags: pd.DataFrame) -> int:
        """Anomalies are a pure function of (data, config): recompute and replace them all."""
        now = _now()
        with self.conn:
            self.conn.execute("DELETE FROM anomalies")
            self.conn.executemany(
                "INSERT OR REPLACE INTO anomalies VALUES (?,?,?,?,?,?,?,?)",
                [(str(f.rate_date), f.bank, f.label, f.method, float(f.score), float(f.threshold), f.reason, now)
                 for f in flags.itertuples()],
            )
            self.conn.execute(
                "UPDATE rates SET status = CASE WHEN EXISTS (SELECT 1 FROM anomalies a WHERE a.rate_date = rates.rate_date"
                " AND a.bank = rates.bank AND a.label = rates.label) THEN 'suspect' ELSE 'ok' END")
        return len(flags)

    # ── reads ─────────────────────────────────────────────────────────────
    def query(self, sql: str, params: tuple = ()) -> pd.DataFrame:
        return pd.read_sql_query(sql, self.conn, params=params)

    def rates_frame(self) -> pd.DataFrame:
        return self.query("SELECT rate_date, bank, label, rate, status, source FROM rates ORDER BY rate_date, bank, label")
