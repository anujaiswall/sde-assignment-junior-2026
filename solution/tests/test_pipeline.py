"""End-to-end: tiny hand-made drops covering the tricky cases, plus idempotency on generated data."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ledger.cli import main

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _secret(monkeypatch, tmp_path):
    monkeypatch.setenv("LEDGER_SECRET", "test-secret")
    monkeypatch.chdir(tmp_path)


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def ev(eid, etype, call, at, phone=None, **payload):
    e = {"event_id": eid, "event_type": etype, "workflow_id": "motor_renewal", "call_id": call,
         "occurred_at": at, "payload": payload}
    if phone:
        e["phone"] = phone
    return json.dumps(e)


def read_csv(p: Path):
    return p.read_text().strip().splitlines()


def make_drops(root: Path):
    write(root / "drop_1/leads/motor_renewal/batch_001.csv",
          "customer_name,phone,policy_number,premium_amount\n"
          "Asha,9876543210,P1,100\n"
          ",,,\n"                                  # blank row: not counted
          "Ravi,12345,P2,\n"                       # invalid phone -> row 2
          "Asha K,+91 98765 43210,P1,Rs. 200\n")   # duplicate of row 1 -> merged
    write(root / "drop_1/events/events_0001.jsonl", "\n".join([
        ev("e1", "call.initiated", "c1", "2026-10-01T18:40:00Z", "9876543210"),   # = 02 Oct IST
        ev("e2", "call.connected", "c1", "2026-10-01T18:40:10Z", "09876543210"),
        ev("e3", "disposition.set", "c1", "2026-10-01T18:45:00Z", "9876543210", disposition_code="renewed"),
        ev("e3", "disposition.set", "c1", "2026-10-01T18:45:00Z", "9876543210", disposition_code="renewed"),
        '{"event_id": "e4", "event_ty',                                         # malformed
        # lead arrives only in drop_2:
        ev("e5", "call.initiated", "c2", "2026-10-03 10:00:00", "7000000001"),
        ev("e6", "call.telephony_failed", "c2", "2026-10-03 10:00:05", "7000000001", reason="x"),
    ]) + "\n")
    write(root / "drop_2/leads/motor_renewal/batch_003.csv",
          "Mobile No,Insured Name,Policy No,extra\n"
          "7000000001,Meena,P3,zzz\n")
    write(root / "drop_2/events/events_1001.jsonl", "\n".join([
        ev("e3", "disposition.set", "c1", "2026-10-01T18:45:00Z", "9876543210", disposition_code="renewed"),
        ev("e7", "disposition.override", "c1", "2026-10-02T09:00:00+05:30", disposition_code="not_interested",
           overridden_by="s1", reason="qa"),
        ev("e8", "disposition.set", "c2", "2026-10-03 10:01:00", "7000000001", disposition_code="RENEWD"),
        ev("e9", "call.initiated", "c9", "2026-10-03 10:00:00", "9000000009"),    # never uploaded
    ]) + "\n")


def test_end_to_end_small(tmp_path):
    make_drops(tmp_path)
    main(["ingest", "drop_1"])
    main(["ingest", "drop_2"])
    main(["ingest", "drop_1", "drop_2"])          # re-run: must change nothing
    main(["report", "--out", "rep"])
    rep = tmp_path / "rep"

    dq = json.loads((rep / "data_quality.json").read_text())
    assert dq == {
        "lead_rows_total": 4, "lead_rows_rejected": 1, "rejections_by_reason": {"invalid_phone": 1},
        "duplicate_lead_rows_merged": 1, "field_warnings": 0, "events_total": 11,
        "malformed_events": 1, "duplicate_events_dropped": 2, "orphan_events": 1,
        "unknown_disposition_events": 1,
    }
    assert read_csv(rep / "rejections.csv")[1:] == ["motor_renewal,batch_001,2,invalid_phone"]
    assert read_csv(rep / "lead_report.csv")[1:] == [
        # override (not_interested) wins over the set (renewed): completed, not a success
        "motor_renewal,P1,batch_001,+91******3210,completed,1,1,2026-10-02T00:10:00+05:30,not_interested,false",
        # events arrived a drop before the lead; still counted
        "motor_renewal,P3,batch_003,+91******0001,in_progress_telephony_failed,1,0,2026-10-03T10:00:00+05:30,,false",
    ]
    assert read_csv(rep / "daily_overview.csv")[1:] == [
        "motor_renewal,2026-10-02,1,1,1.0000,0",
        "motor_renewal,2026-10-03,1,0,0.0000,0",
    ]
    assert read_csv(rep / "batch_summary.csv")[1:] == [
        "motor_renewal,batch_001,1,0,0,0,1",
        "motor_renewal,batch_003,1,0,1,0,0",
    ]


def test_no_plaintext_pii_in_db(tmp_path):
    make_drops(tmp_path)
    main(["ingest", "drop_1", "drop_2"])
    raw = (tmp_path / "ledger.db").read_bytes() + b"".join(
        p.read_bytes() for p in tmp_path.glob("ledger.db-*"))
    for secret in (b"Asha", b"Meena", b"9876543210", b"7000000001"):
        assert secret not in raw


def test_generated_data_incremental_equals_all_at_once(tmp_path):
    subprocess.run([sys.executable, str(REPO / "assignment-a-data/tools/generate_data.py"),
                    "--seed", "5", "--scale", "small", "--out", "data"], check=True, capture_output=True)
    main(["--db", "a.db", "ingest", "data/drop_1"])
    main(["--db", "a.db", "ingest", "data/drop_2"])
    main(["--db", "a.db", "report", "--out", "a1"])
    main(["--db", "a.db", "ingest", "data/drop_1", "data/drop_2"])   # re-run
    main(["--db", "a.db", "report", "--out", "a2"])
    main(["--db", "b.db", "ingest", "data"])
    main(["--db", "b.db", "report", "--out", "b"])
    for name in ("batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv",
                 "data_quality.json"):
        a1 = (tmp_path / "a1" / name).read_text()
        assert a1 == (tmp_path / "a2" / name).read_text(), f"re-ingest changed {name}"
        assert a1 == (tmp_path / "b" / name).read_text(), f"incremental != all-at-once for {name}"
