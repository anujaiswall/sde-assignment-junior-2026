"""Finds the files in a drop and ingests each one exactly once.

Safety model:
* Each file is ingested inside ONE database transaction, together with the row
  in `ingested_files` that marks it done. If the process crashes half-way, the
  transaction rolls back and the file is simply re-done next run.
* Files are identified by the SHA-256 of their content. Re-running `ingest` on
  a drop we already have finds every hash in `ingested_files` and skips it.
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

from .config import Catalogue
from .events import ingest_event_file
from .leads import ingest_lead_file
from .pii import PiiVault


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def discover_drops(path: Path) -> list[Path]:
    """A path is a drop if it has leads/ or events/. Otherwise its drop_* children, in name order."""
    if (path / "leads").is_dir() or (path / "events").is_dir():
        return [path]
    drops = sorted(p for p in path.iterdir() if p.is_dir() and
                   ((p / "leads").is_dir() or (p / "events").is_dir()))
    if not drops:
        raise SystemExit(f"{path}: not a drop (no leads/ or events/) and contains no drops")
    return drops


def _already_ingested(conn: sqlite3.Connection, sha: str) -> bool:
    return conn.execute("SELECT 1 FROM ingested_files WHERE file_sha256 = ?", (sha,)).fetchone() is not None


def _next_seq(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(ingest_seq), 0) + 1 FROM ingested_files").fetchone()[0]


def ingest_drop(conn: sqlite3.Connection, vault: PiiVault, cat: Catalogue, drop: Path,
                log=print) -> dict[str, int]:
    summary = {"files_ingested": 0, "files_skipped": 0}

    # Leads: per workflow folder, files in filename order.
    lead_files = sorted((drop / "leads").glob("*/*.csv")) if (drop / "leads").is_dir() else []
    for path in lead_files:
        wf = path.parent.name
        schema = cat.schemas.get(wf)
        if schema is None:
            print(f"WARNING: no schema for workflow {wf!r}; skipping {path}", file=sys.stderr)
            continue
        sha = file_sha256(path)
        if _already_ingested(conn, sha):
            summary["files_skipped"] += 1
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            s = ingest_lead_file(conn, vault, schema, path, batch_id=path.stem)
            conn.execute(
                "INSERT INTO ingested_files (file_sha256, path, kind, workflow_id, batch_id, ingest_seq,"
                " rows_total, rows_rejected, rows_merged, field_warnings)"
                " VALUES (?, ?, 'leads', ?, ?, ?, ?, ?, ?, ?)",
                (sha, str(path), wf, path.stem, _next_seq(conn),
                 s.rows_total, s.rows_rejected, s.rows_merged, s.field_warnings))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        summary["files_ingested"] += 1
        log(f"  leads  {path}: {s.rows_total} rows, {s.rows_rejected} rejected, "
            f"{s.rows_merged} merged, {s.field_warnings} warnings")

    event_files = sorted((drop / "events").glob("*.jsonl")) if (drop / "events").is_dir() else []
    for path in event_files:
        sha = file_sha256(path)
        if _already_ingested(conn, sha):
            summary["files_skipped"] += 1
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            s = ingest_event_file(conn, vault, path, source=str(path))
            conn.execute(
                "INSERT INTO ingested_files (file_sha256, path, kind, ingest_seq,"
                " lines_total, lines_malformed, lines_duplicate) VALUES (?, ?, 'events', ?, ?, ?, ?)",
                (sha, str(path), _next_seq(conn), s.lines_total, s.lines_malformed, s.lines_duplicate))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        summary["files_ingested"] += 1
        log(f"  events {path}: {s.lines_total} lines, {s.lines_malformed} malformed, "
            f"{s.lines_duplicate} duplicate")
    return summary
