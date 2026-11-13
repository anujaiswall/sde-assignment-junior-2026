"""Lead CSV ingestion: header mapping, validation, rejection, merging."""
from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .config import Field, Schema
from .normalize import normalise_header, normalise_phone, parse_date, parse_decimal, parse_enum
from .pii import PiiVault


# ------------------------------------------------------------------ pure part

def clean_value(f: Field, raw: str) -> str | None:
    """Return the canonical string for a non-empty raw value, or None if invalid."""
    if f.type == "string":
        return raw
    if f.type == "phone":
        return normalise_phone(raw)
    if f.type == "date":
        d = parse_date(raw)
        return d.isoformat() if d else None
    if f.type == "decimal":
        v = parse_decimal(raw)
        return f"{v:.2f}" if v is not None else None
    if f.type == "enum":
        return parse_enum(raw, list(f.values))
    raise ValueError(f"unknown field type {f.type!r}")


@dataclass
class RowResult:
    values: dict[str, str | None] = field(default_factory=dict)  # field name -> clean value / None
    reason: str | None = None       # set => row is rejected
    warnings: int = 0               # optional fields present-but-invalid


def validate_row(schema: Schema, raw: dict[str, str | None]) -> RowResult:
    """Walk fields in schema order; the first failure is the rejection reason.

    `raw` maps field name -> trimmed value ('' / None = missing).
    """
    res = RowResult()
    for f in schema.fields:
        value = raw.get(f.name) or None
        if value is None:
            res.values[f.name] = None
            if f.required and res.reason is None:
                res.reason = f"missing_required:{f.name}"
            continue
        clean = clean_value(f, value)
        res.values[f.name] = clean
        if clean is None:
            if f.type == "phone":
                res.reason = res.reason or "invalid_phone"
            elif f.required:
                res.reason = res.reason or f"invalid_value:{f.name}"   # not in the spec; see SUBMISSION.md
            else:
                res.warnings += 1
    if res.reason:
        res.warnings = 0   # warnings only count for rows that are kept
    return res


def apply_defaults(schema: Schema, values: dict[str, str | None]) -> dict[str, str | None]:
    """Defaults are applied after merging, on read (so a later row can still fill the field)."""
    return {f.name: (values.get(f.name) if values.get(f.name) is not None else f.default)
            for f in schema.fields}


def read_rows(path: Path, schema: Schema):
    """Yield (row_number, {field: trimmed value}) for each non-blank data row."""
    lookup = schema.header_lookup()
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header is None:
            return
        col_to_field: dict[int, str] = {}
        for i, h in enumerate(header):
            name = lookup.get(normalise_header(h))
            if name and name not in col_to_field.values():   # first matching column wins
                col_to_field[i] = name
        row_number = 0
        for cells in reader:
            if all(not c.strip() for c in cells):
                continue        # blank row: skipped, not counted
            row_number += 1
            values = {name: (cells[i].strip() if i < len(cells) else "")
                      for i, name in col_to_field.items()}
            yield row_number, values


# ------------------------------------------------------------------ DB part

@dataclass
class LeadFileStats:
    rows_total: int = 0
    rows_rejected: int = 0
    rows_merged: int = 0
    field_warnings: int = 0


def ingest_lead_file(conn: sqlite3.Connection, vault: PiiVault, schema: Schema,
                     path: Path, batch_id: str) -> LeadFileStats:
    """Process one CSV. Caller wraps this in a transaction."""
    stats = LeadFileStats()
    phone_f = schema.phone_field.name
    ref_f = schema.reference_field.name
    pii_fields = [f.name for f in schema.fields if f.pii and f.name != phone_f]
    plain_fields = [f.name for f in schema.fields if not f.pii]

    for row_number, raw in read_rows(path, schema):
        stats.rows_total += 1
        res = validate_row(schema, raw)
        if res.reason:
            stats.rows_rejected += 1
            conn.execute("INSERT INTO rejections VALUES (?, ?, ?, ?)",
                         (schema.workflow_id, batch_id, row_number, res.reason))
            continue
        stats.field_warnings += res.warnings

        phone = res.values[phone_f]
        phash = vault.phone_hash(phone)
        existing = conn.execute(
            "SELECT lead_id, fields_json, pii_json FROM leads WHERE workflow_id = ? AND phone_hash = ?",
            (schema.workflow_id, phash)).fetchone()

        new_plain = {k: res.values[k] for k in plain_fields}
        new_pii = {k: vault.encrypt(res.values[k]) for k in pii_fields if res.values[k] is not None}

        if existing is None:
            conn.execute(
                "INSERT INTO leads (workflow_id, phone_hash, phone_enc, phone_last4, batch_id,"
                " external_ref, fields_json, pii_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (schema.workflow_id, phash, vault.encrypt(phone), phone[-4:], batch_id,
                 new_plain[ref_f], json.dumps(new_plain, sort_keys=True),
                 json.dumps(new_pii, sort_keys=True)))
        else:
            # Duplicate: overwrite only the fields where the new row has a value.
            stats.rows_merged += 1
            plain = json.loads(existing["fields_json"])
            plain.update({k: v for k, v in new_plain.items() if v is not None})
            pii = json.loads(existing["pii_json"])
            pii.update(new_pii)
            conn.execute(
                "UPDATE leads SET fields_json = ?, pii_json = ?, external_ref = ? WHERE lead_id = ?",
                (json.dumps(plain, sort_keys=True), json.dumps(pii, sort_keys=True),
                 plain[ref_f], existing["lead_id"]))
    return stats
