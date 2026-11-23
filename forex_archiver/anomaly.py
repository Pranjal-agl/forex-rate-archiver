"""Anomaly detection for scraped rate series.

Three detectors, all configurable through Settings:

* ``pct``   simple rule: |change vs last good value| > pct_threshold percent
* ``mad``   robust rule: |log-return - median| / (1.4826 * MAD) > mad_threshold,
            with median/MAD over the last ``mad_window`` *accepted* returns
* ``stale`` the same value repeated for ``stale_days`` or more consecutive observations

Two design choices matter more than the formulas:

1. Each point is compared with the **last accepted** value, not simply the
   previous one. A single bad print (93.2 -> 39.2 -> 93.3) therefore flags only
   the bad day, not the recovery day as well, and a suspect value never becomes
   the baseline for the next one.
2. If the day after a flag agrees with the flagged value rather than with
   the old baseline, the series has genuinely stepped (or the bank changed its
   quoting): the onset stays flagged, and the new level becomes the baseline
   instead of every later day being flagged.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import Settings

MAD_TO_SIGMA = 1.4826
MIN_WINDOW = 5
OUTLIER_K = 5.0          # cross-sectional outlier cut, in robust sigmas
OUTLIER_FLOOR = 0.002    # never treat a 0.2% disagreement between banks as an outlier
REBASE_AFTER = 3   # safety net: accept a new level after this many flags in a row


@dataclass(frozen=True)
class Flag:
    pos: int          # index into the series
    method: str
    score: float
    threshold: float
    reason: str


def detect_jumps(values: np.ndarray, *, method: str, pct_threshold: float, mad_threshold: float,
                 mad_window: int, mad_floor_pct: float, peer_index: np.ndarray | None = None) -> list[Flag]:
    """Online jump detection against the last accepted value. ``method`` is pct, mad or both.

    ``peer_index`` (optional, aligned with ``values``) is the cumulative log
    move of the *other* banks. When given, each move is scored net of what the
    peers did over the same interval, so a market-wide move is not an anomaly
    but a move only this source shows is.
    """
    v = np.asarray(values, dtype=float)
    peer = None if peer_index is None else np.asarray(peer_index, dtype=float)
    flags: list[Flag] = []
    if len(v) < 2:
        return flags
    use_pct = method in ("pct", "both")
    threshold_r = np.log1p(pct_threshold / 100) if use_pct else mad_floor_pct / 100 * mad_threshold
    use_mad = method in ("mad", "both")
    floor = mad_floor_pct / 100.0
    accepted: list[float] = []
    last_good, last_pos = v[0], 0
    pending: list[int] = []
    tag = ""

    for i in range(1, len(v)):
        r = float(np.log(v[i] / last_good))
        if peer is not None and np.isfinite(peer[i]) and np.isfinite(peer[last_pos]):
            r -= float(peer[i] - peer[last_pos])
            tag = " net of peer banks"
        else:
            tag = ""
        hit: list[Flag] = []
        if use_pct:
            pct = abs(np.expm1(r)) * 100
            if pct > pct_threshold:
                hit.append(Flag(i, "pct", round(pct, 4), pct_threshold,
                                f"{pct:.2f}%{tag} vs last good value {last_good:g} (limit {pct_threshold}%)"))
        if use_mad:
            window = np.array(accepted[-mad_window:])
            if len(window) >= MIN_WINDOW:
                med = float(np.median(window))
                scale = max(MAD_TO_SIGMA * float(np.median(np.abs(window - med))), floor)
                z = abs(r - med) / scale
                if z > mad_threshold:
                    hit.append(Flag(i, "mad", round(z, 3), mad_threshold,
                                    f"robust z={z:.1f} on log-return {r * 100:+.2f}%{tag} (limit {mad_threshold})"))
            elif not use_pct:
                # warm-up: too little history for a robust scale, so fall back to the simple rule
                pct = abs(np.expm1(r)) * 100
                if pct > pct_threshold:
                    hit.append(Flag(i, "mad", round(pct, 4), pct_threshold,
                                    f"warm-up: {pct:.2f}% vs last good value {last_good:g} (limit {pct_threshold}%)"))

        if hit and pending and pending[-1] == i - 1 and _consistent(v, i, i - 1, peer, threshold_r):
            # Today agrees with yesterday's flagged value, not with the old baseline: a confirmed level
            # shift. Yesterday's onset stays flagged; today is accepted and becomes the new baseline.
            last_good, last_pos, pending = v[i], i, []
            continue
        if hit:
            flags.extend(hit)
            pending.append(i)
            if len(pending) >= REBASE_AFTER:   # several mutually consistent flags: accept the new level
                last_good, last_pos, pending = v[i], i, []
        else:
            accepted.append(r)
            last_good, last_pos = v[i], i
            pending = []
    return flags


def _consistent(v: np.ndarray, i: int, j: int, peer: np.ndarray | None, limit: float) -> bool:
    r = float(np.log(v[i] / v[j]))
    if peer is not None and np.isfinite(peer[i]) and np.isfinite(peer[j]):
        r -= float(peer[i] - peer[j])
    return abs(r) <= limit


def detect_stale(values: np.ndarray, *, stale_days: int) -> list[Flag]:
    """Flag every repeat in a run of >= stale_days identical values (the first one may be genuine)."""
    v = np.asarray(values, dtype=float)
    flags: list[Flag] = []
    start = 0
    for i in range(1, len(v) + 1):
        if i == len(v) or v[i] != v[start]:
            run = i - start
            if run >= stale_days:
                flags += [Flag(j, "stale", float(run), float(stale_days),
                               f"value {v[start]:g} unchanged for {run} consecutive observations")
                          for j in range(start + 1, i)]
            start = i
    return flags


def _jump_kwargs(settings: Settings) -> dict:
    return {"method": settings.anomaly_method, "pct_threshold": settings.pct_threshold,
            "mad_threshold": settings.mad_threshold, "mad_window": settings.mad_window,
            "mad_floor_pct": settings.mad_floor_pct}


def detect_series(values: np.ndarray, settings: Settings, peer_index: np.ndarray | None = None) -> list[Flag]:
    return (detect_jumps(values, peer_index=peer_index, **_jump_kwargs(settings))
            + detect_stale(values, stale_days=settings.stale_days))


def peer_indexes(df: pd.DataFrame, exclude: set[tuple] | None = None) -> dict[str, pd.Series]:
    """For each bank, the cumulative median log move of the *other* banks, indexed by date.

    Each bank's daily move is the median across its labels (so one bad column
    barely moves it); ``exclude`` holds (rate_date, bank, label) keys to leave
    out, normally the points a first pass already found suspicious. With three
    or more banks, bank-days far from the cross-sectional median are dropped too.
    """
    d = df.sort_values("rate_date").copy()
    d["ret"] = d.groupby(["bank", "label"])["rate"].transform(lambda x: np.log(x).diff())
    if exclude:
        # Blank the return *at* a suspicious point and the one right after it. Differencing across
        # the gap instead would smuggle a peer's spike-recovery or level shift back into the index.
        bad = pd.Series([k in exclude for k in zip(d["rate_date"].astype(str), d["bank"], d["label"], strict=True)],
                        index=d.index)
        after = bad.groupby([d["bank"], d["label"]]).shift(1, fill_value=False)
        d.loc[bad | after, "ret"] = np.nan
    bank_ret = d.dropna(subset=["ret"]).groupby(["rate_date", "bank"])["ret"].median().unstack("bank")
    dates = sorted(df["rate_date"].unique())
    bank_ret = bank_ret.reindex(dates)
    if bank_ret.shape[1] >= 3:
        # With 3+ banks the cross-sectional median is robust to any single bad print, so drop
        # bank-days that sit far from it before the peers are combined (a median of 2 is a mean).
        dev = bank_ret.sub(bank_ret.median(axis=1), axis=0)
        flat = dev.stack().to_numpy()
        tol = max(OUTLIER_K * MAD_TO_SIGMA * float(np.median(np.abs(flat - np.median(flat)))), OUTLIER_FLOOR) if len(flat) else 1
        bank_ret = bank_ret.mask(dev.abs() > tol)
    out = {}
    for bank in df["bank"].unique():
        others = bank_ret.drop(columns=[bank], errors="ignore")
        if others.shape[1] == 0:
            continue
        move = others.median(axis=1, skipna=True)
        if move.notna().sum() == 0:
            continue
        idx = move.fillna(0.0).cumsum()
        idx[~others.notna().any(axis=1).cummax()] = np.nan   # before any peer data exists
        out[bank] = idx
    return out


def detect_frame(df: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    """Run all detectors per (bank, label) series.

    Input columns: rate_date, bank, label, rate. Returns one row per flag with
    rate_date, bank, label, method, score, threshold, reason.

    With ``settings.use_peers`` and more than one bank, detection runs twice:
    pass 1 scores raw moves to find gross errors, pass 2 rebuilds the peer
    index *without* those points and scores every series net of its peers.
    A bad print at one bank therefore cannot leak into the others' baselines.
    Pass-1 flags that most banks share on the same date are treated as a market
    move and stay in the index.
    """
    cols = ["rate_date", "bank", "label", "method", "score", "threshold", "reason"]
    if df.empty:
        return pd.DataFrame(columns=cols)
    df = df.sort_values("rate_date")
    groups = list(df.groupby(["bank", "label"], sort=False))

    def one_pass(peers: dict[str, pd.Series] | None) -> list[dict]:
        out = []
        for (bank, label), g in groups:
            g = g.reset_index(drop=True)
            pidx = None
            if peers and bank in peers:
                pidx = peers[bank].reindex(g["rate_date"]).to_numpy()
            for f in detect_series(g["rate"].to_numpy(), settings, pidx):
                out.append({"rate_date": g.at[f.pos, "rate_date"], "bank": bank, "label": label, "method": f.method,
                            "score": f.score, "threshold": f.threshold, "reason": f.reason})
        return out

    flags = one_pass(None)
    if settings.use_peers and df["bank"].nunique() > 1:
        # Only isolated pass-1 flags are excluded from the peer index. If most banks jumped on the
        # same date, that is the market moving, and it must stay in the index to be netted out.
        banks_present = df.groupby(df["rate_date"].astype(str))["bank"].nunique()
        flagged_banks: dict[str, set] = {}
        for f in flags:
            if f["method"] != "stale":
                flagged_banks.setdefault(str(f["rate_date"]), set()).add(f["bank"])
        market_days = {d for d, b in flagged_banks.items() if len(b) > 1 and len(b) * 2 > banks_present.get(d, 0)}
        suspicious = {(str(f["rate_date"]), f["bank"], f["label"]) for f in flags if str(f["rate_date"]) not in market_days}
        flags = one_pass(peer_indexes(df, exclude=suspicious))
    return pd.DataFrame(flags, columns=cols)
