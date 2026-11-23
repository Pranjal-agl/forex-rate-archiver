"""End to end: fixture-backed sources -> SQLite -> CSV/Excel/JSON -> run report and exit code."""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
from conftest import GOOD_ROUTES, RUN_DATE, fake_fetchers
from openpyxl import load_workbook

from forex_archiver.cli import main
from forex_archiver.db import Database
from forex_archiver.errors import FetchError
from forex_archiver.models import RateRecord
from forex_archiver.pipeline import run

NOSLEEP = lambda s: None  # noqa: E731


def go(settings, routes=GOOD_ROUTES, **kw):
    return run(settings, run_date=RUN_DATE, fetchers=fake_fetchers(routes), sleep=NOSLEEP, **kw)


def test_healthy_run_writes_everything(settings):
    rep = go(settings)
    assert (rep.status, rep.exit_code, rep.needs_alert) == ("ok", 0, False)
    assert {k: v.inserted for k, v in rep.upserts.items()} == {"sbi": 4, "hdfc": 4, "icici": 2}

    csv = pd.read_csv(settings.csv_path)
    assert list(csv.columns) == ["Bank", "Slab_Type", "Rate", "Date", "Status"]
    assert len(csv) == 10 and set(csv.Date) == {"2026-11-20", "2026-11-23"}

    wb = load_workbook(settings.excel_dir / "2026" / "forex_rates_2026-11-23.xlsx")
    assert wb.active.max_row == 11

    dash = json.loads(settings.dashboard_json.read_text())
    assert len(dash["rates"]) == 10 and len(dash["health_latest"]) == 3

    with Database(settings.db_path) as db:
        h = db.query("SELECT source, ok, records, as_of FROM source_health ORDER BY source")
        assert h.ok.tolist() == [1, 1, 1] and h.as_of.tolist() == ["2026-11-23", "2026-11-23", "2026-11-20"]
        assert db.query("SELECT COUNT(*) n FROM raw_snapshots").n[0] == 3
    assert len(list(settings.snapshot_dir.rglob("*.gz"))) == 3


def test_rerun_same_day_is_idempotent(settings):
    go(settings)
    rep = go(settings)
    assert all(v.inserted == 0 and v.unchanged > 0 for v in rep.upserts.values())
    assert len(pd.read_csv(settings.csv_path)) == 10
    with Database(settings.db_path) as db:
        assert db.query("SELECT COUNT(*) n FROM runs").n[0] == 2
        assert db.query("SELECT COUNT(*) n FROM raw_snapshots").n[0] == 3   # identical bytes stored once


def test_partial_failure_exit_2_and_keeps_failed_bytes(settings):
    routes = dict(GOOD_ROUTES)
    routes["icici"] = b"<html><table><tr><td>new layout</td></tr></table>" + b" " * 900 + b"</html>"
    rep = go(settings, routes)
    assert (rep.status, rep.exit_code, rep.needs_alert) == ("partial", 2, True)
    assert [r.source for r in rep.failed] == ["icici"]
    assert "SchemaDriftError" in rep.to_markdown()
    assert list((settings.snapshot_dir / "failures").glob("*icici-SchemaDriftError.html.gz"))


def test_total_failure_exit_1(settings):
    rep = go(settings, {"": FetchError("network down")})
    assert (rep.status, rep.exit_code) == ("failed", 1)
    with Database(settings.db_path) as db:
        assert db.query("SELECT status FROM runs").status[0] == "failed"


def test_spike_today_is_quarantined_and_alerts(settings):
    # 30 days of calm ICICI history around 91.3, then today's real page says 91.28 -> fine.
    # Seed a history near 89 instead, so today's 91.28 is a ~2.6% jump that only ICICI shows.
    with Database(settings.db_path) as db:
        days = pd.bdate_range(end="2026-11-20", periods=30).date
        for bank, label, lvl in [("ICICI", "TT Buying", 89.0), ("ICICI", "TT Selling", 91.9),
                                 ("HDFC", "TT Buying", 90.9), ("SBI", "less than $2,500", 91.9)]:
            db.upsert_rates([RateRecord(bank, label, lvl + 0.01 * (i % 3), d) for i, d in enumerate(days)], source="seed")
    rep = go(settings)
    assert rep.exit_code == 0 and rep.needs_alert
    hit = rep.new_anomalies
    assert ((hit.bank == "ICICI") & (hit.label == "TT Buying")).any()
    with Database(settings.db_path) as db:
        st = db.query("SELECT status FROM rates WHERE bank='ICICI' AND label='TT Buying' AND rate_date='2026-11-23'")
        assert st.status[0] == "suspect"   # kept, but quarantined


def test_stale_feed_is_reported(settings):
    with Database(settings.db_path) as db:
        db.set_headline("SBI", "less than $2,500")
        db.upsert_rates([RateRecord("SBI", "less than $2,500", 91.0, date(2026, 11, 1))], source="seed")
    routes = dict(GOOD_ROUTES)
    routes["sbinewyork"] = FetchError("down")
    rep = go(settings, routes)
    assert rep.exit_code == 2


def test_cli_import_detect_sql(settings, tmp_path, monkeypatch, capsys):
    legacy = tmp_path / "legacy.csv"
    legacy.write_text("Bank,Slab_Type,Rate,Date\n" + "".join(f"HDFC,Cash Buying,94.24,2026-04-{d:02d}\n" for d in range(1, 21))
                      + 'SBI,"  less than   $2,500 ",abc,2026-04-01\n')
    monkeypatch.setenv("FOREX_DB_PATH", str(settings.db_path))
    assert main(["import-csv", str(legacy)]) == 0
    assert "rejected 1" in capsys.readouterr().out
    assert main(["detect"]) == 0
    assert "stale" in capsys.readouterr().out
    assert main(["sql", "SELECT COUNT(*) AS n FROM anomalies WHERE method='stale'"]) == 0
    assert "19" in capsys.readouterr().out
