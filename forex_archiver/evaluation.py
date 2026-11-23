"""Inject known faults into rate series and measure how well each detector finds them.

Base series are geometric random walks quoted to 2 dp, with daily volatility
calibrated from the archive's own clean history. Real history is still too
short (and too gappy) to evaluate on directly. Faults are the failure modes
seen in, or plausible for, scraping:

    small_spike   one-day error of 0.5-1.5%    (digit typo, wrong slab)
    medium_spike  one-day error of 1.5-5%      (buy/sell column swapped)
    large_spike   one-day error of 5-20%       (misaligned row, other currency)
    stale_run     value frozen for 3-15 obs.   (cached/old document, as HDFC was), scored separately
    level_shift   persistent +-2-4% step       (quote convention or slab change)

Scoring is point-level: a flag on an injected point is a TP, a flag on a clean
point is an FP, and an unflagged injected point is an FN. For level_shift only
the first point of the step counts as positive, because after a step the new
level *is* the data. Per-fault "event recall" counts an injection as caught if
any of its points is flagged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

from .anomaly import detect_frame, detect_stale
from .config import Settings

JUMP_FAULTS = ("small_spike", "medium_spike", "large_spike", "level_shift")
FAULTS = JUMP_FAULTS + ("stale_run",)


@dataclass
class Injected:
    values: np.ndarray
    positive: np.ndarray            # bool per point
    events: list[tuple[str, list[int]]]


def estimate_daily_vol(history: pd.DataFrame | None, default: float = 0.0025) -> float:
    """Median per-series std of non-zero log returns over clean rows; falls back to 0.25%/day."""
    if history is None or history.empty:
        return default
    vols = []
    for _, g in history[history["status"] == "ok"].sort_values("rate_date").groupby(["bank", "label"]):
        r = np.diff(np.log(g["rate"].to_numpy()))
        r = r[r != 0]
        if len(r) >= 10:
            vols.append(float(np.std(r)))
    return float(np.median(vols)) if vols else default


def make_series(rng: np.random.Generator, n: int, vol: float, start: float = 92.0) -> np.ndarray:
    """Fat-tailed random walk with volatility regimes (calm / normal / turbulent), quoted to 2 dp.

    Regimes matter: a fixed percent threshold is either too loose for calm
    periods or too tight for turbulent ones, which is what a robust,
    self-scaling rule is supposed to fix.
    """
    mult = np.empty(n)
    i = 0
    while i < n:
        seg = int(rng.integers(30, 90))
        mult[i:i + seg] = rng.choice([0.4, 1.0, 2.2], p=[0.35, 0.45, 0.20])
        i += seg
    mult /= np.sqrt(np.mean(mult**2))                            # keep average vol = calibrated vol
    steps = rng.standard_t(df=4, size=n) * vol / np.sqrt(2) * mult   # t4 has variance 2
    return np.round(start * np.exp(np.cumsum(steps)), 2)


def inject(rng: np.random.Generator, base: np.ndarray, n_faults: int, kinds: tuple[str, ...] = JUMP_FAULTS,
           warmup: int = 25, gap: int = 18, stale_len: tuple[int, int] = (8, 16)) -> Injected:
    v = base.copy()
    pos = np.zeros(len(v), dtype=bool)
    events: list[tuple[str, list[int]]] = []
    offset = int(rng.integers(0, gap))   # per-series offset: faults at different banks are independent
    slots = list(range(warmup + offset, len(v) - 16, gap))
    for start in rng.choice(slots, size=min(n_faults, len(slots)), replace=False):
        kind = kinds[rng.integers(len(kinds))]
        sign = rng.choice([-1, 1])
        if kind.endswith("spike"):
            lo, hi = {"small_spike": (0.005, 0.015), "medium_spike": (0.015, 0.05), "large_spike": (0.05, 0.20)}[kind]
            v[start] = round(v[start] * (1 + sign * rng.uniform(lo, hi)), 2)
            idx = [start]
        elif kind == "stale_run":
            length = int(rng.integers(*stale_len))
            v[start:start + length] = v[start - 1]
            idx = list(range(start, start + length))
        else:  # level_shift
            v[start:] = np.round(v[start:] * (1 + sign * rng.uniform(0.02, 0.04)), 2)
            idx = [start]
        pos[idx] = True
        events.append((kind, idx))
    return Injected(v, pos, events)


def score(flags_pos: set[int], inj: Injected) -> dict:
    truth = set(np.flatnonzero(inj.positive))
    tp, fp, fn = len(flags_pos & truth), len(flags_pos - truth), len(truth - flags_pos)
    caught = {k: [0, 0] for k in {kind for kind, _ in inj.events}}
    for kind, idx in inj.events:
        caught[kind][1] += 1
        caught[kind][0] += int(any(i in flags_pos for i in idx))
    return {"tp": tp, "fp": fp, "fn": fn, "caught": caught}


def _ratio(a: int, b: int) -> float:
    return round(a / b, 3) if b else 0.0


def make_market(rng: np.random.Generator, n: int, vol: float, n_banks: int = 3, idio: float = 0.35) -> list[np.ndarray]:
    """Correlated bank series: one shared market path, plus per-bank markup and timing noise.

    Each bank's quote = market level x (1 + markup) x exp(noise), with iid noise
    of ``idio`` x vol per day. That noise stands in for banks fixing their
    rates at different times of day.
    """
    market = np.log(make_series(rng, n, vol))
    return [np.round(np.exp(market + rng.uniform(-0.01, 0.01) + rng.normal(0, idio * vol, n)), 2)
            for _ in range(n_banks)]


def _frame(series: list[np.ndarray]) -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-01", periods=len(series[0])).date
    return pd.concat([pd.DataFrame({"rate_date": dates, "bank": f"B{b}", "label": "TT Buying", "rate": v})
                      for b, v in enumerate(series)], ignore_index=True)


def evaluate(settings: Settings, *, history: pd.DataFrame | None = None, n_series: int = 120, length: int = 260,
             faults_per_series: int = 6, seed: int = 7) -> pd.DataFrame:
    """Sweep pct and mad thresholds, each with and without the peer-bank adjustment.

    Each dataset is three correlated banks with independent faults injected
    into every bank, scored through the production ``detect_frame`` path.
    Only jump faults are injected here; the stale rule is scored separately.
    """
    vol = estimate_daily_vol(history)
    rng = np.random.default_rng(seed)
    datasets = []
    for _ in range(n_series):
        injs = [inject(rng, base, faults_per_series) for base in make_market(rng, length, vol)]
        datasets.append((_frame([x.values for x in injs]), injs))

    grid = [("pct", t) for t in (0.5, 0.75, 1.0, 1.5, 2.0, 3.0)] + [("mad", t) for t in (3, 4, 5, 6, 8, 10, 12)]
    rows = []
    for peers in (False, True):
        for method, thr in grid:
            cfg = replace(settings, anomaly_method=method, use_peers=peers, stale_days=10**6,
                          **({"pct_threshold": thr} if method == "pct" else {"mad_threshold": thr}))
            tp = fp = fn = 0
            caught = {k: [0, 0] for k in JUMP_FAULTS}
            for frame, injs in datasets:
                flags = detect_frame(frame, cfg)
                pos_by_bank = {f"B{b}": {d: i for i, d in enumerate(frame.loc[frame.bank == f"B{b}", "rate_date"])}
                               for b in range(len(injs))}
                for b, inj in enumerate(injs):
                    fb = flags[flags.bank == f"B{b}"]
                    s = score({pos_by_bank[f"B{b}"][d] for d in fb["rate_date"]}, inj)
                    tp, fp, fn = tp + s["tp"], fp + s["fp"], fn + s["fn"]
                    for k, (c, n) in s["caught"].items():
                        caught[k][0] += c
                        caught[k][1] += n
            p, r = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
            rows.append({"method": method + ("+peers" if peers else ""), "threshold": thr, "precision": p, "recall": r,
                         "f1": round(2 * p * r / (p + r), 3) if p + r else 0.0, "tp": tp, "fp": fp, "fn": fn,
                         **{f"recall_{k}": _ratio(c, n) for k, (c, n) in caught.items()}})
    out = pd.DataFrame(rows)
    n_points = n_series * 3 * length
    out.attrs.update(daily_vol=vol, n_series=n_series, length=length, faults_per_series=faults_per_series, seed=seed,
                     clean_points=n_points - n_series * 3 * faults_per_series, n_banks=3, idio=0.35)
    return out


def evaluate_stale(settings: Settings, *, n_series: int = 200, length: int = 260, seed: int = 11,
                   vol: float = 0.0025) -> pd.DataFrame:
    """Sweep stale_days on frozen runs of 3-15 observations. Recall is per frozen point, precision per flag."""
    rng = np.random.default_rng(seed)
    datasets = [inject(rng, make_series(rng, length, vol), 6, kinds=("stale_run",), stale_len=(3, 16))
                for _ in range(n_series)]
    rows = []
    for k in (3, 4, 5, 7, 10):
        tp = fp = fn = 0
        for inj in datasets:
            s = score({f.pos for f in detect_stale(inj.values, stale_days=k)}, inj)
            tp, fp, fn = tp + s["tp"], fp + s["fp"], fn + s["fn"]
        p, r = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
        rows.append({"stale_days": k, "precision": p, "recall": r, "f1": round(2 * p * r / (p + r), 3) if p + r else 0.0,
                     "tp": tp, "fp": fp, "fn": fn})
    return pd.DataFrame(rows)


def write_report(df: pd.DataFrame, stale: pd.DataFrame, settings: Settings, path: Path) -> None:
    a = df.attrs
    best = df.loc[df.groupby("method")["f1"].idxmax()]
    per_year = lambda fp: fp / a["clean_points"] * 250  # noqa: E731  false alarms per series per year
    cur = df[(df.method.str.startswith("pct") & (df.threshold == settings.pct_threshold))
             | (df.method.str.startswith("mad") & (df.threshold == settings.mad_threshold))]
    lines = [
        "# Anomaly detector evaluation on injected faults",
        "",
        "Generated by `forex-archiver evaluate` (deterministic; re-run after changing a detector). "
        f"{a['n_series']} synthetic markets x {a['n_banks']} correlated banks x {a['length']} business days. The shared "
        f"market path is a fat-tailed (t4) random walk with calm, normal and turbulent volatility regimes, average daily vol "
        f"{a['daily_vol'] * 100:.2f}% calibrated from the archive. Each bank adds a fixed markup and iid timing noise "
        f"of {a['idio']}x vol, quoted to 2 dp. {a['faults_per_series']} independent faults per bank series, seed {a['seed']}. "
        "Scored through the production `detect_frame` path.",
        "",
        "Positives are the injected points: one per spike, and the first day of a level shift. "
        "Precision is flags on injected points divided by all flags. Recall is the share of injected points flagged.",
        "",
        "## Jump detectors: best threshold per method (by F1)",
        "",
        "| method | threshold | precision | recall | F1 | false alarms per series-year |",
        "|---|---|---|---|---|---|",
    ]
    lines += [f"| {r.method} | {r.threshold:g} | {r.precision:.3f} | {r.recall:.3f} | {r.f1:.3f} | {per_year(r.fp):.1f} |"
              for r in best.itertuples()]
    top = df.loc[df["f1"].idxmax()]
    lines += ["", f"Best overall: **{top.method} @ {top.threshold:g}** (F1 {top.f1:.3f}).", "",
              "Configured defaults (`forex_archiver.toml`):", "",
              "| method | threshold | precision | recall | F1 | false alarms per series-year |", "|---|---|---|---|---|---|"]
    lines += [f"| {r.method} | {r.threshold:g} | {r.precision:.3f} | {r.recall:.3f} | {r.f1:.3f} | {per_year(r.fp):.1f} |"
              for r in cur.itertuples()]
    lines += ["", "## Full sweep (recall by fault type)", "",
              "| method | thr | P | R | F1 | " + " | ".join(JUMP_FAULTS) + " |",
              "|---|---|---|---|---|" + "---|" * len(JUMP_FAULTS)]
    for r in df.itertuples():
        lines.append(f"| {r.method} | {r.threshold:g} | {r.precision:.3f} | {r.recall:.3f} | {r.f1:.3f} | "
                     + " | ".join(f"{getattr(r, 'recall_' + k):.2f}" for k in JUMP_FAULTS) + " |")
    lines += ["", "Fault sizes: small 0.5-1.5%, medium 1.5-5%, large 5-20%, level shift 2-4%.", "",
              "## Stale-value rule", "",
              "Frozen runs of 3-15 observations injected into clean series. Recall counts frozen points "
              "(the first point of a run is genuine, so it is not a positive).", "",
              "| stale_days | precision | recall | F1 |", "|---|---|---|---|"]
    lines += [f"| {r.stale_days} | {r.precision:.3f} | {r.recall:.3f} | {r.f1:.3f} |" for r in stale.itertuples()]
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    path.with_suffix(".json").write_text(json.dumps(
        {"meta": a, "jump": df.to_dict("records"), "stale": stale.to_dict("records")}, indent=1, default=float))
