"""SQLite schema. See SUBMISSION.md for the reasoning behind each table."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
-- One row per input file we have fully ingested. Its presence is what makes
-- re-running `ingest` a no-op (idempotency), and it stores the per-file
-- counters that data_quality.json is summed from.
CREATE TABLE IF NOT EXISTS ingested_files (
    file_sha256     TEXT PRIMARY KEY,          -- identity = content, not path
    path            TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('leads', 'events')),
    workflow_id     TEXT,                      -- leads files only
    batch_id        TEXT,                      -- leads files only (file stem)
    ingest_seq      INTEGER NOT NULL,          -- order files were ingested in
    rows_total      INTEGER NOT NULL DEFAULT 0,
    rows_rejected   INTEGER NOT NULL DEFAULT 0,
    rows_merged     INTEGER NOT NULL DEFAULT 0,
    field_warnings  INTEGER NOT NULL DEFAULT 0,
    lines_total     INTEGER NOT NULL DEFAULT 0,
    lines_malformed INTEGER NOT NULL DEFAULT 0,
    lines_duplicate INTEGER NOT NULL DEFAULT 0,
    ingested_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Current, merged state of each lead. Identity = (workflow_id, phone).
-- No plaintext PII: the phone is stored as a keyed hash (for joining) plus an
-- encrypted copy; other PII fields are encrypted inside pii_json.
CREATE TABLE IF NOT EXISTS leads (
    lead_id       INTEGER PRIMARY KEY,
    workflow_id   TEXT NOT NULL,
    phone_hash    TEXT NOT NULL,
    phone_enc     TEXT NOT NULL,
    phone_last4   TEXT NOT NULL,               -- needed for phone_masked
    batch_id      TEXT NOT NULL,               -- batch of the FIRST valid row, never changes
    external_ref  TEXT NOT NULL,
    fields_json   TEXT NOT NULL,               -- non-PII fields, merged, nulls kept (defaults applied on read)
    pii_json      TEXT NOT NULL,               -- {field: fernet_token} for PII fields
    UNIQUE (workflow_id, phone_hash)
);

-- Rejected lead rows. Deliberately no PII: only where it was and why.
CREATE TABLE IF NOT EXISTS rejections (
    workflow_id TEXT NOT NULL,
    batch_id    TEXT NOT NULL,
    row_number  INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    PRIMARY KEY (workflow_id, batch_id, row_number)
);

-- The interaction ledger. Append-only: every well-formed, first-seen event is
-- stored exactly as received (phone replaced by its hash). Linking to leads is
-- NOT stored here; it is computed at report time, so an event that arrived
-- before its lead is linked automatically once the lead exists.
CREATE TABLE IF NOT EXISTS events (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,   -- arrival order
    event_id         TEXT NOT NULL UNIQUE,                -- dedupe key, across all drops
    event_type       TEXT,
    workflow_id      TEXT,
    call_id          TEXT,
    occurred_at_utc  TEXT,                                -- fixed-width UTC, sortable as text
    phone_hash       TEXT,                                -- NULL if absent or not a valid mobile
    disposition_code TEXT,
    payload_json     TEXT NOT NULL,
    source_file      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_link ON events (workflow_id, phone_hash);
CREATE INDEX IF NOT EXISTS ix_events_call ON events (call_id);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events ledger is append-only'); END;

-- Every ledger event that can be tied to a lead, with its lead_id.
--  * normal events link by (workflow_id, phone_hash)
--  * disposition.override has no phone: it inherits the lead of its call's
--    other events (preferring the call.initiated event's lead).
DROP VIEW IF EXISTS linked_events;
CREATE VIEW linked_events AS
WITH direct AS (
    SELECT e.seq, l.lead_id, e.call_id, e.event_type
    FROM events e
    JOIN leads l ON l.workflow_id = e.workflow_id AND l.phone_hash = e.phone_hash
    WHERE e.event_type <> 'disposition.override'
),
call_lead AS (
    SELECT call_id,
           COALESCE(MIN(CASE WHEN event_type = 'call.initiated' THEN lead_id END),
                    MIN(lead_id)) AS lead_id
    FROM direct
    WHERE call_id IS NOT NULL
    GROUP BY call_id
)
SELECT seq, lead_id FROM direct
UNION ALL
SELECT e.seq, cl.lead_id
FROM events e JOIN call_lead cl ON cl.call_id = e.call_id
WHERE e.event_type = 'disposition.override';
"""


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)  # we manage transactions explicitly
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
