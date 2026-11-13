"""Builds the five report files from the database.

The database does the heavy lifting of *linking* (the `linked_events` view
joins events to leads by phone hash, and overrides through their call_id).
Python then turns linked events into calls and applies the business rules,
which live in small pure functions so they can be unit-tested directly.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from .config import Catalogue
from .normalize import IST, from_utc_key

BUCKETS = ("yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed")


# ------------------------------------------------------------------ pure rules

@dataclass
class DispositionEvent:
    event_type: str          # disposition.set | disposition.override
    code: str
    occurred_at: datetime
    seq: int                 # arrival order, used only to break exact-time ties


def effective_disposition(events: list[DispositionEvent]) -> str | None:
    """Latest override wins; otherwise latest set; otherwise None.

    `events` must already exclude codes that aren't in the catalogue.
    """
    for kind in ("disposition.override", "disposition.set"):
        candidates = [e for e in events if e.event_type == kind]
        if candidates:
            return max(candidates, key=lambda e: (e.occurred_at, e.seq)).code
    return None


@dataclass
class Call:
    call_id: str
    lead_id: int
    initiated_at: datetime
    connected: bool = False
    telephony_failed: bool = False
    dispositions: list[DispositionEvent] = field(default_factory=list)

    @property
    def effective(self) -> str | None:
        return effective_disposition(self.dispositions)


def lead_bucket(calls: list[Call], terminal_codes: set[str]) -> str:
    """First matching rule wins."""
    if not calls:
        return "yet_to_reach_out"
    if any(c.effective in terminal_codes for c in calls):
        return "completed"
    if all(c.telephony_failed for c in calls):
        return "in_progress_telephony_failed"
    return "in_progress_dialed"


def ratio_4dp(num: int, den: int) -> str:
    return str((Decimal(num) / Decimal(den)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def ist_iso(dt: datetime) -> str:
    return dt.astimezone(IST).isoformat(timespec="seconds")


def mask_phone(last4: str) -> str:
    return "+91******" + last4


# ------------------------------------------------------------------ build model

@dataclass
class Model:
    leads: list[sqlite3.Row]
    calls_by_lead: dict[int, list[Call]]
    orphan_events: int
    unknown_disposition_events: int


def build_model(conn: sqlite3.Connection, cat: Catalogue) -> Model:
    leads = conn.execute(
        "SELECT lead_id, workflow_id, batch_id, external_ref, phone_last4 FROM leads").fetchall()
    lead_wf = {r["lead_id"]: r["workflow_id"] for r in leads}

    rows = conn.execute(
        "SELECT le.lead_id, e.seq, e.event_type, e.call_id, e.occurred_at_utc, e.disposition_code"
        " FROM linked_events le JOIN events e ON e.seq = le.seq").fetchall()
    total_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    linked_seqs = {r["seq"] for r in rows}

    by_call: dict[str, list[sqlite3.Row]] = defaultdict(list)
    unknown = 0
    for r in rows:
        et = r["event_type"] or ""
        if et.startswith("disposition."):
            if r["disposition_code"] not in cat.dispositions.get(lead_wf[r["lead_id"]], {}):
                unknown += 1
                continue                      # ignored from here on
        by_call[r["call_id"] if r["call_id"] is not None else f"__noid_{r['seq']}"].append(r)

    calls_by_lead: dict[int, list[Call]] = defaultdict(list)
    for call_id, evs in by_call.items():
        inits = [e for e in evs if e["event_type"] == "call.initiated"]
        if not inits:
            continue                          # no call.initiated => not a call
        first = min(inits, key=lambda e: (e["occurred_at_utc"], e["seq"]))
        call = Call(call_id, first["lead_id"], from_utc_key(first["occurred_at_utc"]))
        for e in evs:
            et = e["event_type"]
            if et == "call.connected":
                call.connected = True
            elif et == "call.telephony_failed":
                call.telephony_failed = True
            elif et in ("disposition.set", "disposition.override"):
                call.dispositions.append(DispositionEvent(
                    et, e["disposition_code"], from_utc_key(e["occurred_at_utc"]), e["seq"]))
        calls_by_lead[call.lead_id].append(call)

    return Model(leads, calls_by_lead, total_events - len(linked_seqs), unknown)


# ------------------------------------------------------------------ writers

def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def _b(v: bool) -> str:
    return "true" if v else "false"


def write_reports(conn: sqlite3.Connection, cat: Catalogue, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    m = build_model(conn, cat)
    terminal = {wf: {c for c, d in ds.items() if d.is_terminal} for wf, ds in cat.dispositions.items()}
    success = {wf: {c for c, d in ds.items() if d.is_success} for wf, ds in cat.dispositions.items()}

    # 1 + 3: batch summary and lead report
    batch_counts: dict[tuple[str, str], Counter] = defaultdict(Counter)
    lead_rows = []
    for lead in m.leads:
        wf = lead["workflow_id"]
        calls = sorted(m.calls_by_lead.get(lead["lead_id"], []), key=lambda c: (c.initiated_at, c.call_id))
        bucket = lead_bucket(calls, terminal.get(wf, set()))
        batch_counts[(wf, lead["batch_id"])][bucket] += 1
        with_disp = [c for c in calls if c.effective is not None]
        lead_rows.append([
            wf, lead["external_ref"], lead["batch_id"], mask_phone(lead["phone_last4"]), bucket,
            len(calls), sum(c.connected for c in calls),
            ist_iso(calls[-1].initiated_at) if calls else "",
            with_disp[-1].effective if with_disp else "",
            _b(any(c.effective in success.get(wf, set()) for c in calls)),
        ])
    lead_rows.sort(key=lambda r: (r[0], r[1], r[3]))
    _write_csv(out / "lead_report.csv",
               ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls",
                "connected_calls", "last_call_at", "current_disposition", "ever_success"], lead_rows)

    _write_csv(out / "batch_summary.csv",
               ["workflow_id", "batch_id", "total_leads", *BUCKETS],
               [[wf, b, sum(c.values()), *(c[k] for k in BUCKETS)]
                for (wf, b), c in sorted(batch_counts.items())])

    # 2: daily overview
    lead_wf = {lead["lead_id"]: lead["workflow_id"] for lead in m.leads}
    days: dict[tuple[str, str], dict] = defaultdict(lambda: {"total": 0, "connected": 0, "success": set()})
    for lead_id, calls in m.calls_by_lead.items():
        wf = lead_wf[lead_id]
        for c in calls:
            d = days[(wf, c.initiated_at.astimezone(IST).date().isoformat())]
            d["total"] += 1
            d["connected"] += c.connected
            if c.effective in success.get(wf, set()):
                d["success"].add(lead_id)
    _write_csv(out / "daily_overview.csv",
               ["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate", "success_leads"],
               [[wf, day, d["total"], d["connected"], ratio_4dp(d["connected"], d["total"]), len(d["success"])]
                for (wf, day), d in sorted(days.items())])

    # 4: rejections (no PII stored, none written)
    rej = conn.execute("SELECT workflow_id, batch_id, row_number, reason FROM rejections"
                       " ORDER BY workflow_id, batch_id, row_number").fetchall()
    _write_csv(out / "rejections.csv", ["workflow_id", "batch_id", "row_number", "reason"],
               [list(r) for r in rej])

    # 5: data quality
    s = conn.execute(
        "SELECT COALESCE(SUM(rows_total),0), COALESCE(SUM(rows_rejected),0), COALESCE(SUM(rows_merged),0),"
        " COALESCE(SUM(field_warnings),0), COALESCE(SUM(lines_total),0), COALESCE(SUM(lines_malformed),0),"
        " COALESCE(SUM(lines_duplicate),0) FROM ingested_files").fetchone()
    by_reason = dict(conn.execute(
        "SELECT reason, COUNT(*) FROM rejections GROUP BY reason ORDER BY reason").fetchall())
    dq = {
        "lead_rows_total": s[0],
        "lead_rows_rejected": s[1],
        "rejections_by_reason": by_reason,
        "duplicate_lead_rows_merged": s[2],
        "field_warnings": s[3],
        "events_total": s[4],
        "malformed_events": s[5],
        "duplicate_events_dropped": s[6],
        "orphan_events": m.orphan_events,
        "unknown_disposition_events": m.unknown_disposition_events,
    }
    (out / "data_quality.json").write_text(json.dumps(dq, indent=2) + "\n", encoding="utf-8")
