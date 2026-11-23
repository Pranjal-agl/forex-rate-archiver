"""Orchestrator: sources -> raw snapshots -> upsert -> anomaly flags -> exports -> run report."""

from __future__ import annotations

import gzip
import json
import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .anomaly import detect_frame
from .config import Settings
from .db import Database, UpsertStats
from .export import write_csv, write_daily_excel, write_dashboard_json
from .models import SourceResult
from .sources import REGISTRY, Source

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def today_ist() -> date:
    return datetime.now(IST).date()


@dataclass
class RunReport:
    run_id: int
    run_date: date
    results: list[SourceResult]
    upserts: dict[str, UpsertStats] = field(default_factory=dict)
    new_anomalies: pd.DataFrame = field(default_factory=pd.DataFrame)
    stale_series: list[dict] = field(default_factory=list)

    @property
    def failed(self) -> list[SourceResult]:
        return [r for r in self.results if not r.ok]

    @property
    def status(self) -> str:
        if not any(r.ok for r in self.results):
            return "failed"
        return "partial" if self.failed else "ok"

    @property
    def exit_code(self) -> int:
        """0 healthy | 1 every source failed | 2 some source failed or a feed is stale."""
        if self.status == "failed":
            return 1
        if self.failed or self.stale_series:
            return 2
        return 0

    @property
    def needs_alert(self) -> bool:
        return self.exit_code != 0 or not self.new_anomalies.empty

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id, "run_date": self.run_date.isoformat(), "status": self.status,
            "exit_code": self.exit_code, "needs_alert": self.needs_alert,
            "sources": [{"source": r.source, "ok": r.ok, "records": len(r.records), "latency_s": round(r.latency_s, 2),
                         "attempts": r.attempts, "error_type": r.error_type, "error": r.error,
                         "as_of": max((x.rate_date for x in r.records), default=None) and
                         max(x.rate_date for x in r.records).isoformat()} for r in self.results],
            "upserts": {k: vars(v) for k, v in self.upserts.items()},
            "new_anomalies": json.loads(self.new_anomalies.to_json(orient="records", date_format="iso"))
            if not self.new_anomalies.empty else [],
            "stale_series": self.stale_series,
        }

    def to_markdown(self) -> str:
        icon = {"ok": "✅", "partial": "⚠️", "failed": "❌"}[self.status]
        lines = [f"### {icon} Forex archiver run #{self.run_id} ({self.run_date}): **{self.status}**", "",
                 "| Source | OK | Records | As of | Latency | Attempts | Error |", "|---|---|---|---|---|---|---|"]
        for s in self.to_dict()["sources"]:
            err = f"`{s['error_type']}`: {s['error']}" if s["error_type"] else ""
            lines.append(f"| {s['source']} | {'✅' if s['ok'] else '❌'} | {s['records']} | {s['as_of'] or '-'} | "
                         f"{s['latency_s']}s | {s['attempts']} | {err} |")
        if self.stale_series:
            lines += ["", "**Stale feeds** (newest clean value older than allowed):"]
            lines += [f"- {s['bank']} / {s['label']}: last {s['latest_date']} ({s['age_days']} days)" for s in self.stale_series]
        if not self.new_anomalies.empty:
            lines += ["", "**Quarantined as suspect in this run:**"]
            lines += [f"- {a.rate_date} {a.bank} / {a.label}: {a.method}: {a.reason}" for a in self.new_anomalies.itertuples()]
        return "\n".join(lines) + "\n"


def run(
    settings: Settings,
    source_names: list[str] | None = None,
    *,
    run_date: date | None = None,
    trigger: str = "manual",
    fetchers: dict | None = None,
    sleep: Callable[[float], None] = time.sleep,
    write_excel: bool = True,
) -> RunReport:
    run_date = run_date or today_ist()
    sources: list[Source] = [REGISTRY[n]() for n in (source_names or list(REGISTRY))]

    with Database(settings.db_path) as db:
        for s in sources:
            db.set_headline(s.bank, s.headline_label)
        run_id = db.start_run(trigger=trigger, git_sha=os.environ.get("GITHUB_SHA"))

        def work(src: Source) -> SourceResult:
            return src.run(today=run_date, attempts=settings.fetch_attempts, timeout=settings.request_timeout_s,
                           fetchers=fetchers, sleep=sleep)

        with ThreadPoolExecutor(max_workers=len(sources)) as pool:
            results = list(pool.map(work, sources))

        report = RunReport(run_id, run_date, results)
        touched: set[tuple[str, str, str]] = set()
        for res in results:
            sha = None
            if res.snapshot is not None:
                sha = db.save_snapshot(res.snapshot, run_id, settings.snapshot_dir)
                if not res.ok:   # keep the exact bytes that broke the parser, in git, for the regression test
                    _keep_failure(res, settings.snapshot_dir)
            db.record_health(run_id, res, sha)
            if res.ok:
                report.upserts[res.source] = db.upsert_rates(res.records, source=res.source, snapshot=sha)
                touched |= {(r.rate_date.isoformat(), r.bank, r.label) for r in res.records}
            logger.info("%-6s ok=%s records=%d %s", res.source, res.ok, len(res.records), res.error or "")

        flags = detect_frame(db.rates_frame(), settings)
        db.replace_anomalies(flags)
        if not flags.empty and touched:
            keys = flags[["rate_date", "bank", "label"]].astype(str).apply(tuple, axis=1)
            report.new_anomalies = flags[keys.isin(touched)].reset_index(drop=True)

        fresh = db.query("SELECT f.* FROM v_freshness f JOIN headline_labels h USING(bank, label)")
        names = {s.bank for s in sources}
        report.stale_series = [
            {**r, "age_days": (run_date - date.fromisoformat(r["latest_date"])).days}
            for r in fresh.to_dict("records")
            if r["bank"] in names and (run_date - date.fromisoformat(r["latest_date"])).days > settings.max_staleness_days
        ]

        write_csv(db, settings.csv_path)
        if write_excel:
            write_daily_excel(db, settings.excel_dir, run_date, sorted({k[0] for k in touched}))
        write_dashboard_json(db, settings.dashboard_json)
        db.finish_run(run_id, report.status)
    return report


def _keep_failure(res: SourceResult, root: Path) -> None:
    snap = res.snapshot
    assert snap is not None
    dest = root / "failures" / f"{snap.fetched_at:%Y-%m-%d}-{snap.source}-{res.error_type}.{snap.kind}.gz"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(dest, "wb") as f:
        f.write(snap.content)
