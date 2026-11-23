"""Anomaly detectors on hand-built and injected series."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from forex_archiver.anomaly import detect_frame, detect_jumps, detect_stale
from forex_archiver.config import Settings
from forex_archiver.evaluation import evaluate, evaluate_stale

KW = dict(pct_threshold=1.5, mad_threshold=6.0, mad_window=20, mad_floor_pct=0.15)


def walk(n=60, seed=0, vol=0.002):
    rng = np.random.default_rng(seed)
    return np.round(92 * np.exp(np.cumsum(rng.normal(0, vol, n))), 2)


@pytest.mark.parametrize("method", ["pct", "mad"])
def test_single_bad_print_flags_only_that_day(method):
    v = walk()
    v[40] = round(v[40] * 1.08, 2)  # e.g. wrong currency row
    flags = detect_jumps(v, method=method, **KW)
    assert [f.pos for f in flags] == [40], "the recovery day must not be flagged"


@pytest.mark.parametrize("method", ["pct", "mad"])
def test_level_shift_flags_onset_then_rebaselines(method):
    v = walk()
    v[30:] = np.round(v[30:] * 1.03, 2)
    assert [f.pos for f in detect_jumps(v, method=method, **KW)] == [30]


def test_decimal_shift_is_huge_score():
    v = walk()
    v[25] = v[25] / 10
    f = detect_jumps(v, method="mad", **KW)
    assert f[0].pos == 25 and f[0].score > 100


def test_stale_run_flags_repeats_but_not_first_value():
    v = walk(40)
    v[10:22] = v[10]
    flags = detect_stale(v, stale_days=5)
    assert [f.pos for f in flags] == list(range(11, 22))
    assert detect_stale(walk(40), stale_days=5) == []


def test_threshold_is_configurable_via_env_and_toml(tmp_path, monkeypatch):
    cfg = tmp_path / "c.toml"
    cfg.write_text('anomaly_method = "pct"\npct_threshold = 0.4\n')
    monkeypatch.setenv("FOREX_MAD_THRESHOLD", "9.5")
    s = Settings.load(cfg)
    assert (s.anomaly_method, s.pct_threshold, s.mad_threshold) == ("pct", 0.4, 9.5)
    monkeypatch.setenv("FOREX_PCT_THRESHOLD", "2.5")
    assert Settings.load(cfg).pct_threshold == 2.5  # env beats file

    v = walk(seed=3)
    v[45] = round(v[45] * 1.01, 2)  # 1% bad print
    loose, tight = replace(s, pct_threshold=2.0), replace(s, pct_threshold=0.8)
    assert 45 not in {f.pos for f in detect_jumps(v, method="pct", pct_threshold=loose.pct_threshold, mad_threshold=6,
                                                   mad_window=20, mad_floor_pct=0.15)}
    assert 45 in {f.pos for f in detect_jumps(v, method="pct", pct_threshold=tight.pct_threshold, mad_threshold=6,
                                               mad_window=20, mad_floor_pct=0.15)}


def _frame(series: dict[str, np.ndarray]) -> pd.DataFrame:
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(len(next(iter(series.values()))))]
    return pd.concat([pd.DataFrame({"rate_date": days, "bank": b, "label": "TT Buying", "rate": v})
                      for b, v in series.items()], ignore_index=True)


def test_peers_excuse_market_wide_move_but_catch_one_bank():
    base = walk(80, vol=0.001)
    rng = np.random.default_rng(1)
    banks = {b: np.round(base * (1 + k * 0.004) * np.exp(rng.normal(0, 0.0003, 80)), 2) for k, b in enumerate("ABC")}
    for b in banks:                      # market drops 2.5% on day 50 at every bank
        banks[b][50:] = np.round(banks[b][50:] * 0.975, 2)
    banks["B"][65] = round(banks["B"][65] * 1.02, 2)   # B alone prints a bad value
    s = Settings(anomaly_method="mad")
    with_peers = detect_frame(_frame(banks), s)
    without = detect_frame(_frame(banks), replace(s, use_peers=False))
    assert set(map(str, without.rate_date)) >= {"2026-02-20"}            # day 50 flagged when blind to peers
    flagged = {(str(r.rate_date), r.bank) for r in with_peers.itertuples()}
    assert not any(d == "2026-02-20" for d, _ in flagged)              # market move excused
    assert ("2026-03-07", "B") in flagged                              # idiosyncratic error still caught


def test_frozen_legacy_hdfc_pattern_is_flagged():
    v = np.full(36, 94.24)
    df = _frame({"HDFC": v})
    out = detect_frame(df, Settings())
    assert (out.method == "stale").sum() == 35


def test_evaluation_reports_sane_precision_recall():
    df = evaluate(Settings(), n_series=6, length=200)
    assert {"pct", "mad", "pct+peers", "mad+peers"} <= set(df.method)
    assert df.precision.between(0, 1).all() and df.recall.between(0, 1).all()
    # looser thresholds catch more
    pct = df[df.method == "pct"].sort_values("threshold")
    assert pct.recall.is_monotonic_decreasing
    st = evaluate_stale(Settings(), n_series=10)
    assert st.loc[st.stale_days == 5, "recall"].item() > 0.9


def test_long_outage_restarts_baseline_instead_of_flagging_drift():
    """Regression: after the May-Nov outage, Nov values were flagged as 3.4% jumps vs May."""
    days = [date(2026, 5, 1) + timedelta(days=i) for i in range(15)] + [date(2026, 11, 20), date(2026, 11, 23)]
    rates = [95.0 + 0.01 * (i % 3) for i in range(15)] + [91.87, 91.90]
    df = pd.DataFrame({"rate_date": days, "bank": "SBI", "label": "x", "rate": rates})
    assert detect_frame(df, Settings()).empty
    df.loc[16, "rate"] = 96.5   # but a real jump right after the restart is still caught
    assert detect_frame(df, Settings()).rate_date.tolist() == [date(2026, 11, 23)]
