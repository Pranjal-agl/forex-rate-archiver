"""Derived outputs. The database is the source of truth; these files are regenerated from it."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .db import Database

HEADER_FILL = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
ALT_FILL = PatternFill(start_color="EBF3FB", end_color="EBF3FB", fill_type="solid")
SUSPECT_FILL = PatternFill(start_color="FDE2E1", end_color="FDE2E1", fill_type="solid")


def write_csv(db: Database, path: Path) -> int:
    df = db.rates_frame().rename(columns={"bank": "Bank", "label": "Slab_Type", "rate": "Rate",
                                          "rate_date": "Date", "status": "Status"})
    df[["Bank", "Slab_Type", "Rate", "Date", "Status"]].to_csv(path, index=False)
    return len(df)


def write_daily_excel(db: Database, root: Path, run_date: date, rate_dates: list[str]) -> Path | None:
    """One workbook per run, holding the rows this run touched plus their day-over-day delta."""
    if not rate_dates:
        return None
    marks = ",".join("?" * len(rate_dates))
    df = db.query(
        f"SELECT bank, label, rate, rate_date, prev_rate, delta, pct_change, status FROM v_daily_delta "
        f"WHERE rate_date IN ({marks}) ORDER BY bank, label, rate_date", tuple(rate_dates))
    path = root / str(run_date.year) / f"forex_rates_{run_date.isoformat()}.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    ws = wb.active
    ws.title = "Forex Rates"
    ws.append(["Bank", "Slab_Type", "Rate", "Date", "Prev_Rate", "Delta", "Pct_Change", "Status"])
    for row in df.itertuples(index=False):
        ws.append([None if pd.isna(x) else x for x in row])
    for cell in ws[1]:
        cell.fill, cell.font, cell.alignment = HEADER_FILL, Font(color="FFFFFF", bold=True), Alignment(horizontal="center")
    for i, row in enumerate(ws.iter_rows(min_row=2), start=2):
        fill = SUSPECT_FILL if row[-1].value == "suspect" else (ALT_FILL if i % 2 == 0 else None)
        for cell in row:
            if fill:
                cell.fill = fill
            cell.alignment = Alignment(horizontal="center")
    for col in ws.columns:
        ws.column_dimensions[get_column_letter(col[0].column)].width = max(len(str(c.value or "")) for c in col) + 4
    wb.save(path)
    return path


def write_dashboard_json(db: Database, path: Path) -> None:
    """Everything the static dashboard needs, in one file it can fetch."""
    def rows(sql: str) -> list[dict]:
        return json.loads(db.query(sql).to_json(orient="records"))

    payload = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rates": rows("SELECT rate_date AS d, bank AS b, label AS l, rate AS r, status AS s, delta AS dd, pct_change AS pc "
                      "FROM v_daily_delta ORDER BY rate_date"),
        "headline": rows("SELECT bank, label FROM headline_labels"),
        "spread": rows("SELECT rate_date, banks, spread, detail FROM v_bank_spread ORDER BY rate_date"),
        "anomalies": rows("SELECT * FROM anomalies ORDER BY rate_date DESC LIMIT 200"),
        "health_latest": rows("SELECT h.*, r.started_at FROM source_health h JOIN runs r USING(run_id) "
                              "WHERE run_id = (SELECT MAX(run_id) FROM source_health)"),
        "reliability": rows("SELECT * FROM v_source_reliability"),
        "freshness": rows("SELECT f.* FROM v_freshness f JOIN headline_labels h USING(bank, label)"),
        "runs": rows("SELECT run_id, started_at, status FROM runs ORDER BY run_id DESC LIMIT 60"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, separators=(",", ":")))
