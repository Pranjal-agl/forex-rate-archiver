"""Command line entry point: ``forex-archiver <command>`` or ``python -m forex_archiver``."""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import sys
from pathlib import Path

import pandas as pd

from .anomaly import detect_frame
from .cleaning import normalise
from .config import Settings
from .db import Database
from .errors import ArchiverError
from .sources import REGISTRY

logger = logging.getLogger("forex_archiver")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("pdfminer").setLevel(logging.WARNING)
    logging.getLogger("selenium").setLevel(logging.WARNING)


def cmd_run(args, s: Settings) -> int:
    from .pipeline import run
    report = run(s, args.banks, trigger=args.trigger, write_excel=not args.no_excel)
    Path("run_report.json").write_text(json.dumps(report.to_dict(), indent=2, default=str))
    Path("run_report.md").write_text(report.to_markdown())
    print(report.to_markdown())
    return report.exit_code


def cmd_import_csv(args, s: Settings) -> int:
    from .models import RateRecord
    clean, rejected = normalise(pd.read_csv(args.path, dtype=str), s)
    with Database(s.db_path) as db:
        stats = db.upsert_rates([RateRecord(r.bank, r.label, float(r.rate), r.rate_date) for r in clean.itertuples()],
                                source="legacy-csv")
        for cls in REGISTRY.values():
            db.set_headline(cls.bank, cls.headline_label)
        db.replace_anomalies(detect_frame(db.rates_frame(), s))
    print(f"imported {len(clean)} rows ({vars(stats)}), rejected {len(rejected)}")
    return 0


def cmd_detect(args, s: Settings) -> int:
    with Database(s.db_path) as db:
        flags = detect_frame(db.rates_frame(), s)
        if not args.dry_run:
            db.replace_anomalies(flags)
    summary = flags.groupby(["bank", "label", "method"]).size() if not flags.empty else "no anomalies"
    print(f"method={s.anomaly_method} pct={s.pct_threshold} mad={s.mad_threshold} stale_days={s.stale_days}")
    print(summary)
    return 0


def cmd_evaluate(args, s: Settings) -> int:
    from .evaluation import evaluate, evaluate_stale, write_report
    hist = None
    if s.db_path.exists():
        with Database(s.db_path) as db:
            hist = db.rates_frame()
    df = evaluate(s, history=hist, n_series=args.series, seed=args.seed)
    stale = evaluate_stale(s, n_series=args.series)
    write_report(df, stale, s, Path(args.out))
    print(df[["method", "threshold", "precision", "recall", "f1"]].to_string(index=False))
    print(stale.to_string(index=False))
    print(f"\nreport -> {args.out}")
    return 0


def cmd_replay(args, s: Settings) -> int:
    """Re-parse archived snapshots (e.g. after a parser fix) and optionally upsert the result."""
    src = REGISTRY[args.source]()
    ok = bad = 0
    with Database(s.db_path) as db:
        for path in sorted(Path(args.dir).rglob(f"{args.source}-*.gz")):
            try:
                sheet = src.parse(gzip.open(path).read())
            except ArchiverError as exc:
                bad += 1
                print(f"✗ {path}: {type(exc).__name__}: {exc}")
                continue
            ok += 1
            print(f"✓ {path}: {sheet.as_of} {[(r.label, r.rate) for r in sheet.records]}")
            if args.write:
                db.upsert_rates(sheet.records, source=f"replay:{src.name}")
        if args.write:
            db.replace_anomalies(detect_frame(db.rates_frame(), s))
    print(f"{ok} parsed, {bad} failed")
    return 1 if bad and not ok else 0


def cmd_export(args, s: Settings) -> int:
    from .export import write_csv, write_dashboard_json
    with Database(s.db_path) as db:
        n = write_csv(db, s.csv_path)
        write_dashboard_json(db, s.dashboard_json)
    print(f"wrote {n} rows -> {s.csv_path}, dashboard -> {s.dashboard_json}")
    return 0


def cmd_sql(args, s: Settings) -> int:
    with Database(s.db_path) as db:
        print(db.query(args.query).to_string(index=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="forex-archiver", description="USD/INR multi-bank rate archiver")
    p.add_argument("--config", help="TOML settings file (default: forex_archiver.toml if present)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="fetch all sources and update the archive")
    r.add_argument("--banks", nargs="+", choices=list(REGISTRY), default=None)
    r.add_argument("--no-excel", action="store_true")
    r.add_argument("--trigger", default="manual")
    r.set_defaults(fn=cmd_run)

    i = sub.add_parser("import-csv", help="load a legacy all_rates_history.csv into the database")
    i.add_argument("path")
    i.set_defaults(fn=cmd_import_csv)

    d = sub.add_parser("detect", help="recompute anomaly flags with the current thresholds")
    d.add_argument("--dry-run", action="store_true")
    d.set_defaults(fn=cmd_detect)

    e = sub.add_parser("evaluate", help="score detectors on injected faults (precision/recall)")
    e.add_argument("--series", type=int, default=120)
    e.add_argument("--seed", type=int, default=7)
    e.add_argument("--out", default="reports/anomaly_eval.md")
    e.set_defaults(fn=cmd_evaluate)

    rp = sub.add_parser("replay", help="re-parse archived raw snapshots")
    rp.add_argument("source", choices=list(REGISTRY))
    rp.add_argument("dir")
    rp.add_argument("--write", action="store_true", help="upsert parsed rows into the database")
    rp.set_defaults(fn=cmd_replay)

    x = sub.add_parser("export", help="rewrite the CSV and dashboard JSON from the database")
    x.set_defaults(fn=cmd_export)

    q = sub.add_parser("sql", help="run a read query against the archive")
    q.add_argument("query")
    q.set_defaults(fn=cmd_sql)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    return args.fn(args, Settings.load(args.config))


if __name__ == "__main__":
    sys.exit(main())
