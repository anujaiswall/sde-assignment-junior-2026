"""Event JSONL ingestion into the append-only ledger.

At ingest time we only do the checks that never change: malformed and
duplicate. Orphan / unknown-disposition depend on which leads exist, which can
change with a later drop, so they are decided at report time.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .normalize import normalise_phone, parse_timestamp, to_utc_key
from .pii import PiiVault


@dataclass
class EventFileStats:
    lines_total: int = 0
    lines_malformed: int = 0
    lines_duplicate: int = 0


def parse_line(line: str) -> dict | None:
    """Return the event dict, or None if the line is malformed."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    eid = obj.get("event_id")
    if eid is None or (isinstance(eid, str) and not eid.strip()):
        return None
    return obj


def ingest_event_file(conn: sqlite3.Connection, vault: PiiVault, path: Path,
                      source: str) -> EventFileStats:
    stats = EventFileStats()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            stats.lines_total += 1
            ev = parse_line(line)
            if ev is None:
                stats.lines_malformed += 1
                continue

            occurred = parse_timestamp(ev.get("occurred_at"))
            if occurred is None:
                # Not in the spec's definition of malformed, but an event with no
                # usable time can't be placed on a date or ordered. See SUBMISSION.md.
                stats.lines_malformed += 1
                continue
            phone = normalise_phone(ev["phone"]) if isinstance(ev.get("phone"), str) else None
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            code = payload.get("disposition_code")

            cur = conn.execute(
                "INSERT OR IGNORE INTO events (event_id, event_type, workflow_id, call_id,"
                " occurred_at_utc, phone_hash, disposition_code, payload_json, source_file)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(ev["event_id"]), ev.get("event_type"), ev.get("workflow_id"), ev.get("call_id"),
                 to_utc_key(occurred),
                 vault.phone_hash(phone) if phone else None,
                 code if isinstance(code, str) else None,
                 json.dumps(payload, sort_keys=True, ensure_ascii=False), source))
            if cur.rowcount == 0:      # UNIQUE(event_id) hit: seen before, in any drop
                stats.lines_duplicate += 1
    return stats
