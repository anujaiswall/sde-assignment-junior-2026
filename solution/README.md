# Solution: Assignment A (Interaction Ledger & Reporting Pipeline)

See `../SUBMISSION.md` for run commands, design and decisions.

```
solution/
├── run.py              # entry point: python solution/run.py ingest|report ...
├── ledger/
│   ├── cli.py          # argument parsing
│   ├── config.py       # loads schemas + dispositions.csv
│   ├── normalize.py    # phone / date / decimal / enum / header / timestamp rules (pure)
│   ├── leads.py        # CSV reading, row validation, rejection, merge
│   ├── events.py       # JSONL -> append-only ledger (malformed + duplicate checks)
│   ├── ingest.py       # finds files in a drop, one transaction per file, idempotency
│   ├── pii.py          # HMAC phone hashing + Fernet encryption
│   ├── db.py           # SQLite schema, append-only triggers, linked_events view
│   └── report.py       # calls, effective disposition, buckets, the 5 output files
└── tests/              # pytest
```
