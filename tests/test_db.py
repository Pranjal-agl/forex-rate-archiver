"""Storage: idempotent upsert, constraints, SQL analytics views."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from forex_archiver.db import Database
from forex_archiver.models import RateRecord


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "t.db") as d:
        yield d


def rec(day: int, rate: float, bank="SBI", label="TT Buying") -> RateRecord:
    return RateRecord(bank, label, rate, date(2026, 11, 1) + timedelta(days=day))


def count(db) -> int:
    return db.conn.execute("SELECT COUNT(*) FROM rates").fetchone()[0]


def test_upsert_is_idempotent(db):
    rows = [rec(0, 91.0), rec(1, 91.2), rec(2, 91.1)]
    first = db.upsert_rates(rows, source="t")
    second = db.upsert_rates(rows, source="t")
    assert (first.inserted, second.inserted, second.unchanged) == (3, 0, 3)
    assert count(db) == 3


def test_revision_updates_value_and_counts(db):
    db.upsert_rates([rec(0, 91.0)], source="t")
    stats = db.upsert_rates([rec(0, 91.5)], source="t")
    row = db.conn.execute("SELECT rate, revisions FROM rates").fetchone()
    assert stats.revised == 1 and tuple(row) == (91.5, 1)


def test_constraints_reject_bad_rows(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("INSERT INTO rates(rate_date,bank,label,rate,source,first_seen_at,last_seen_at)"
                        " VALUES ('2026-11-01','SBI','x',-1,'t','n','n')")
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("INSERT INTO rates(rate_date,bank,label,rate,source,first_seen_at,last_seen_at)"
                        " VALUES ('01/11/2026','SBI','x',91,'t','n','n')")


def test_daily_delta_view_uses_previous_observation(db):
    db.upsert_rates([rec(0, 90.0), rec(3, 90.9), rec(4, 90.0)], source="t")  # gap on days 1-2
    d = db.query("SELECT rate_date, prev_date, delta, pct_change FROM v_daily_delta ORDER BY rate_date")
    assert d.prev_date.tolist()[1] == "2026-11-01"
    assert d.delta.tolist()[1:] == [0.9, -0.9]
    assert round(d["pct_change"].tolist()[1], 2) == 1.0


def test_spread_view_uses_headline_labels_and_skips_suspect(db):
    db.set_headline("SBI", "TT Buying")
    db.set_headline("HDFC", "TT Buying")
    db.upsert_rates([rec(0, 91.0), rec(0, 91.6, bank="HDFC"), rec(0, 99.0, bank="HDFC", label="Cash Selling")], source="t")
    sp = db.query("SELECT spread, banks FROM v_bank_spread").iloc[0]
    assert (round(sp.spread, 2), sp.banks) == (0.6, 2)


@settings(max_examples=60, deadline=None)
@given(st.lists(st.tuples(st.integers(0, 5), st.sampled_from(["SBI", "HDFC"]), st.floats(80, 100).map(lambda x: round(x, 2))),
                min_size=1, max_size=40))
def test_property_upsert_equals_last_write_wins(tmp_path_factory, ops):
    """Any sequence of writes, duplicates included, leaves exactly a last-write-wins map."""
    with Database(tmp_path_factory.mktemp("p") / "p.db") as d:
        expected = {}
        for day, bank, rate in ops:
            d.upsert_rates([rec(day, rate, bank=bank)], source="t")
            expected[(rec(day, rate).rate_date.isoformat(), bank)] = rate
        got = {(r["rate_date"], r["bank"]): r["rate"] for r in d.conn.execute("SELECT * FROM rates")}
        assert got == expected
