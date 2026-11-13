"""Command line entry point.

    python solution/run.py [--db ledger.db] ingest <drop-or-folder> [more ...]
    python solution/run.py [--db ledger.db] report --out reports/
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from .config import DEFAULT_SCHEMA_DIR, load_catalogue
from .db import connect
from .ingest import discover_drops, ingest_drop
from .pii import PiiVault, load_or_create_secret
from .report import write_reports


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ledger", description="Interaction ledger & reporting pipeline")
    ap.add_argument("--db", default="ledger.db", help="SQLite database file (default: ledger.db)")
    ap.add_argument("--schemas", default=str(DEFAULT_SCHEMA_DIR), help="folder with schemas + dispositions.csv")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_ing = sub.add_parser("ingest", help="ingest one or more drops (or a folder of drops)")
    p_ing.add_argument("paths", nargs="+", type=Path)
    p_rep = sub.add_parser("report", help="write the five report files")
    p_rep.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)

    cat = load_catalogue(Path(args.schemas))
    conn = connect(args.db)
    t0 = time.perf_counter()
    if args.cmd == "ingest":
        vault = PiiVault(load_or_create_secret())
        for p in args.paths:
            for drop in discover_drops(p):
                print(f"ingesting {drop}")
                s = ingest_drop(conn, vault, cat, drop)
                print(f"  -> {s['files_ingested']} file(s) ingested, {s['files_skipped']} already done")
    else:
        write_reports(conn, cat, args.out)
        print(f"reports written to {args.out}/")
    print(f"done in {time.perf_counter() - t0:.2f}s")


if __name__ == "__main__":
    main()
