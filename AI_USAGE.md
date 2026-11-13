# AI Usage Disclosure

## Tools used
Claude (Anthropic), used as a pair-programmer and tutor.

## What I used them for
- Reading the spec and comparing assignments A and B before I picked A, because it suits my current Python level and can be tested deterministically.
- Writing the first version of the pipeline in `solution/ledger/` and the tests in `solution/tests/`.
- Writing a separate, naive reference implementation (not committed) to cross-check all five reports across several seeds.
- Drafting `SUBMISSION.md`.
- Walking me through the code file by file afterwards (`cli.py`, `config.py`, `db.py`, `pii.py`, `normalize.py`) and brushing up the Python and SQL basics I needed to read it.

## What I wrote or decided myself
- Chose Assignment A over B after comparing the two.
- Reviewed the design choices with Claude and kept the ones I could explain and defend:
  - linking events to leads at report time rather than ingest time (so events that arrive before their lead are handled)
  - HMAC rather than plain SHA-256 for phones (a plain hash of a 10-digit phone can be brute-forced)
  - Fernet encryption for names
  - one transaction per file plus a content-hash table for idempotent re-runs
  - SQLite triggers to keep the ledger append-only
- Worked through each module by tracing real examples by hand (e.g. how `"91-98765-43210"` and `"022-2345-6789"` move through `normalise_phone`).


## Where the AI was wrong or unhelpful
- One of the generated tests claimed `919876543210` is an invalid phone. It's actually valid: 12 digits starting with `91`, so the `91` is dropped. The test failed, and checking the spec's phone rule showed the test was wrong, not the code.
- The first explanations of the code were too dense for me, with many concepts at once. I asked for a step-by-step walkthrough, one small piece at a time, which worked much better.

## How I verified AI-generated code
- 73 pytest tests covering phone/date/decimal parsing, rejection order, effective disposition, bucket logic, idempotency and PII at rest.
- Diffed all five reports against an independently written reference implementation on 5 small seeds and the large dataset: identical.
- Checked the two worked examples in the spec (`0.5625`, `2026-10-14T17:55:24+05:30`) against our output.
- Searched the database file for known names and phone numbers to confirm they aren't stored in plain text, and confirmed that UPDATE on the ledger is blocked.
- Ran the pipeline myself and traced functions by hand in the Python REPL.
