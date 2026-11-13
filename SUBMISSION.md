# Submission

## Candidate
- **Name:** _your name_
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** _fill in_

## How to run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

python solution/run.py ingest data/drop_1
python solution/run.py ingest data/drop_2
python solution/run.py report --out reports/

pytest                      # 73 tests, ~1s
```
- `--db PATH` (default `ledger.db`) picks the SQLite file. Delete it to start from scratch.
- `ingest` accepts several drops (`ingest data/drop_1 data/drop_2`) or a folder of drops (`ingest data`).
- PII secret: taken from `LEDGER_SECRET`. If that's unset, one is generated into `./.env` (git-ignored) on the first ingest. **Use the same secret for every run against one database**, because the phone hashes depend on it.

## Design

```
CSV / JSONL files ──► ingest (one DB transaction per file) ──► SQLite ──► report (5 files)
                         │                                       │
              validate, normalise, reject,          leads, rejections, events (ledger),
              merge leads; dedupe events            ingested_files (what's done + counters)
```

**Tables** (`solution/ledger/db.py`):

| Table | Purpose | Key constraint |
|---|---|---|
| `ingested_files` | One row per input file fully processed, with its counters (rows, rejected, merged, warnings, lines, malformed, duplicates). | PK = SHA-256 of file content |
| `leads` | Current merged state of each lead. | `UNIQUE(workflow_id, phone_hash)` = lead identity |
| `rejections` | Where a bad row was and why. **No PII columns at all.** | PK `(workflow_id, batch_id, row_number)` |
| `events` | **The interaction ledger.** Every well-formed, first-seen event, as received (phone replaced by its hash). | `UNIQUE(event_id)`; triggers abort any `UPDATE`/`DELETE` |
| `linked_events` (view) | Joins events to leads: by `(workflow_id, phone_hash)`, and overrides through their `call_id`. | — |

**Key idea: linking is computed, not stored.** At ingest time I only make the decisions that can never change: is the line malformed, and has this `event_id` been seen before? Whether an event is an orphan depends on which leads exist, and that changes when a later drop brings the lead. So the ledger keeps every event, and linking happens in the `linked_events` view at report time. An event that was an orphan after drop 1 links automatically once its lead arrives. It also means "drop 1 then drop 2" and "everything at once" give the same reports by construction. I verified that on 6 seeds.

**PII** (`solution/ledger/pii.py`):
- Phone → `HMAC-SHA256(secret, "+91XXXXXXXXXX")`. The same phone always gives the same hash, so events can be joined to leads with plain SQL equality. A plain SHA-256 would be unsafe: there are only about 4 billion Indian mobile numbers, which can be brute-forced in minutes. The HMAC key prevents that.
- Phone (recoverable copy) and `customer_name` → encrypted with Fernet (AES-128-CBC + HMAC). The hash key and the encryption key are derived separately from one secret.
- `phone_last4` is stored in plain text so `phone_masked` can be built without decrypting. Like the last 4 digits of a card number, it doesn't identify a person on its own.
- Rejected rows store no values at all, only the reason.

**Idempotency and crash safety:** each file is processed inside one `BEGIN IMMEDIATE … COMMIT` transaction. That transaction also inserts the file's row into `ingested_files`. Either the whole file and its "done" marker commit together, or nothing does. If the process is killed half-way, SQLite rolls back and the next run redoes that file. Re-running a drop finds every file hash already present and skips it. Event dedupe is also enforced by `UNIQUE(event_id)` with `INSERT OR IGNORE`, which covers duplicates within a file and across drops.

**Rules as pure functions:** phone/date/decimal parsing (`normalize.py`), row validation (`leads.validate_row`), `effective_disposition` and `lead_bucket` (`report.py`) have no database access, so they're unit-tested directly.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| A file's identity is the SHA-256 of its content, not its path | The same file re-delivered under another path is still skipped. A file whose content changed is treated as new. | Path-based: misses re-deliveries, and silently ignores corrected files |
| Orphan / unknown-disposition decided at report time, not ingest time | Leads can arrive after their events, and the spec wants final-state counts | Re-scanning orphans after each drop: more state, same result |
| A **required** non-phone field that is present but invalid → reject with `invalid_value:<field>` | The spec only defines missing-required and invalid-phone. The current schemas never trigger this (required fields are strings or phones). | Treat it as `missing_required:<field>` |
| An optional phone field that is present but invalid → still `invalid_phone` (reject) | The spec lists invalid phone as a rejection reason without saying "required" | Null + warning |
| If two CSV columns map to the same field, the first (left-most) wins | Deterministic, simple | Last wins / error |
| Field warnings on **duplicate** rows are counted | They are valid, non-rejected rows, and the spec counts warnings for every non-rejected row | Counting only rows that create a lead |
| An event whose `occurred_at` can't be parsed counts as **malformed** | It can't be placed on a date or ordered. Never happens in the generated data. | Keep it with a null time |
| Override → lead: through the call's `call.initiated` event's lead, else any linked event of that call | The spec says "the lead of that call's other events". The initiated event is the one that defines the call. | — |
| A call belongs to the lead of its `call.initiated`. `connected` / `telephony_failed` / dispositions come from any linked event with that `call_id`. | Matches "a call is counted only if its initiated event is present" | — |
| Unknown disposition codes are checked against the **lead's** workflow catalogue, and only for linked events | The spec processes orphan before unknown, so an orphan with a bad code counts once, as an orphan | — |
| Ties on `occurred_at` between dispositions → the later-arriving event wins (`seq`) | Deterministic. Last-received is the most plausible correction. | `event_id` order |
| Ties on `call.initiated` time for `current_disposition` → `call_id` order | Deterministic | — |
| `rejections_by_reason` lists only reasons that occurred, keys sorted | The template's zeros look like placeholders | Always listing every possible reason with 0 |
| `batch_summary` has one row per batch that owns ≥1 lead | Leads are what the table counts. A batch whose rows were all rejected/duplicates has no leads. | Emit zero rows for every ingested file |
| Defaults (`preferred_language → en`) are applied on read (`leads.apply_defaults`), not stored | The spec says defaults apply *after* merging. Storing nulls keeps that true across drops. | Writing the default at insert time |
| `connectivity_rate` uses `Decimal` with ROUND_HALF_UP | Avoids float/banker's-rounding surprises at the 4th decimal | `f"{x:.4f}"` on a float |
| SQLite in WAL mode | Zero setup, real transactions and constraints. Fast enough for the large scale. | Postgres (in docker-compose): better for concurrent writers in production |

## Failure modes considered
- **Re-running a drop:** every file hash is already in `ingested_files`, so nothing changes (tested).
- **Crash mid-file:** one transaction per file, so it rolls back and is redone on the next run.
- **Duplicate events within a file / across drops:** `UNIQUE(event_id)` + `INSERT OR IGNORE`, counted.
- **Malformed lines** (truncated JSON, non-object JSON, missing `event_id`): counted, skipped.
- **Events before their lead:** linked at report time (tested: orphan count goes 1123 → 462 for seed 42 once drop 2 arrives).
- **Dirty CSVs:** BOM (read as `utf-8-sig`), renamed/re-ordered/extra columns, short rows, blank rows.
- **Someone tries to edit the ledger:** SQLite triggers abort `UPDATE`/`DELETE` on `events`.
- **Not handled:**
  - Losing or changing `LEDGER_SECRET` makes existing phone hashes unmatchable. There's no key rotation.
  - Two `ingest` processes at once: SQLite's `BEGIN IMMEDIATE` serialises them, but this isn't tested.
  - A corrected version of an already-ingested lead file is treated as a new file, so its rows count as merged duplicates.
  - No schema migrations.

## Testing
- `pytest` runs 73 tests in about 1s:
  - `test_normalize.py`: all six phone formats plus invalid ones, the four date formats, impossible dates, leap years, currency prefixes, negatives, rounding, header normalisation, the three timestamp forms.
  - `test_rules.py`: schema-order rejection, warnings vs rejections, defaults, effective disposition (override beats set, latest by time not arrival), every bucket rule and their precedence, 4-dp formatting.
  - `test_pipeline.py`: a hand-built two-drop dataset covering duplicates, blank rows, malformed lines, cross-drop duplicate events, phone-less overrides, an unknown code, an event before its lead, a pure orphan, and the UTC→IST date shift. Also checks for no plaintext PII in the DB file, and incremental == all-at-once == re-run on generated data.
- **Cross-check:** I also wrote a separate, naive in-memory implementation of the spec (no DB, written differently) and diffed all five outputs. They were byte-identical on seeds 42, 7, 123, 2026, 99 (small) and 42 (large). This catches pipeline/SQL bugs. It can't catch a misreading of the spec that both versions share. The two examples printed in the spec (`0.5625` on 2026-10-01; `2026-10-14T17:55:24+05:30` for MOT/2026/100000) both match.
- **Not tested:** concurrent ingests, a mid-file kill (relying on SQLite's transaction guarantees), Postgres.

## Results
- Seed 42 small: ingest about 0.25s total, report about 0.1s.
- Seed 42 **large** (140k leads, 600k events): ingest **37.5s**, report **9.1s**, total about 47s (under the 2-min stretch target).
- Seed 42 `data_quality.json`: 2057 lead rows, 108 rejected, 57 merged, 122 warnings; 8745 event lines, 8 malformed, 237 duplicates, 462 orphans, 18 unknown dispositions.
- Surprising in the data: a duplicate event can be delivered in a *later* drop. Some leads' rows exist only in batch 003 even though their calls happened before drop 1's cutoff, which is why orphans drop by more than half after drop 2. And because of the UTC→IST shift, calls at e.g. 18:40Z land on the next IST date.

**Stretch goals done:** performance (large in under 2 min); linking in SQL (the `linked_events` view).

**Production at 100k leads/day:** Postgres instead of SQLite. Run ingestion as a scheduled job (Airflow/cron) triggered per drop, with the same per-file transaction and content-hash idempotency. Keep the PII secret in a KMS/secrets manager. Add an `ingest_runs` audit table and alert on rejection/orphan rates spiking. Backfills are safe because ingest is idempotent: re-drop the files. Make reports incremental by materialising per-call facts keyed by `call_id` and recomputing only calls touched by new events.

## If I had another day
1. Materialise a `calls` table incrementally, so reports don't rescan the whole ledger (true incremental reporting).
2. Key rotation for the PII secret (store a key version next to each hash/ciphertext).
3. A `reingest --force <file>` command for corrected files, plus clearer handling of a changed file at the same path.
4. A Postgres backend behind the same interface, and a test that runs two ingests concurrently.
5. Property-based tests (Hypothesis) for the phone/date/decimal parsers.
