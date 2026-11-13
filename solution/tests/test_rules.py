"""Row validation, effective disposition and bucket logic."""
from datetime import datetime, timedelta, timezone

import pytest

from ledger.config import load_catalogue
from ledger.leads import apply_defaults, validate_row
from ledger.report import Call, DispositionEvent, effective_disposition, lead_bucket, ratio_4dp

CAT = load_catalogue()
MOTOR = CAT.schemas["motor_renewal"]
GOOD = {"customer_name": "A B", "phone": "9876543210", "policy_number": "P1",
        "policy_expiry": "01/12/2026", "premium_amount": "₹1,000", "preferred_language": "HI"}


def test_valid_row_is_normalised():
    r = validate_row(MOTOR, GOOD)
    assert r.reason is None and r.warnings == 0
    assert r.values == {"customer_name": "A B", "phone": "+919876543210", "policy_number": "P1",
                        "policy_expiry": "2026-12-01", "premium_amount": "1000.00",
                        "preferred_language": "hi"}


def test_first_failure_in_schema_order_wins():
    # name missing (field 1) beats invalid phone (field 2)
    assert validate_row(MOTOR, {**GOOD, "customer_name": "", "phone": "123"}).reason == \
        "missing_required:customer_name"
    # invalid phone (field 2) beats missing policy number (field 3)
    assert validate_row(MOTOR, {**GOOD, "phone": "123", "policy_number": ""}).reason == "invalid_phone"
    assert validate_row(MOTOR, {**GOOD, "phone": ""}).reason == "missing_required:phone"


def test_optional_invalid_gives_null_and_warning():
    r = validate_row(MOTOR, {**GOOD, "policy_expiry": "31/02/2026", "preferred_language": "Hindi"})
    assert r.reason is None
    assert r.warnings == 2
    assert r.values["policy_expiry"] is None and r.values["preferred_language"] is None


def test_optional_empty_is_not_a_warning():
    assert validate_row(MOTOR, {**GOOD, "premium_amount": ""}).warnings == 0


def test_no_warnings_counted_for_rejected_rows():
    r = validate_row(MOTOR, {**GOOD, "phone": "N/A", "premium_amount": "N/A"})
    assert r.reason == "invalid_phone" and r.warnings == 0


def test_defaults_apply_only_to_null():
    v = apply_defaults(MOTOR, {"preferred_language": None})
    assert v["preferred_language"] == "en"
    assert apply_defaults(MOTOR, {"preferred_language": "ta"})["preferred_language"] == "ta"


T0 = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)


def d(kind, code, minutes, seq=0):
    return DispositionEvent(f"disposition.{kind}", code, T0 + timedelta(minutes=minutes), seq)


def test_effective_disposition():
    assert effective_disposition([]) is None
    assert effective_disposition([d("set", "a", 0), d("set", "b", 5)]) == "b"
    # arrival order doesn't matter, occurred_at does
    assert effective_disposition([d("set", "b", 5), d("set", "a", 0)]) == "b"
    # any override beats any set, even an older override vs a newer set
    assert effective_disposition([d("override", "x", 1), d("set", "b", 99)]) == "x"
    assert effective_disposition([d("override", "x", 10), d("override", "y", 2)]) == "x"


TERMINAL = {c for c, x in CAT.dispositions["motor_renewal"].items() if x.is_terminal}


def call(failed=False, connected=False, disp=None):
    c = Call("c", 1, T0, connected=connected, telephony_failed=failed)
    if disp:
        c.dispositions.append(d("set", disp, 0))
    return c


@pytest.mark.parametrize("calls,bucket", [
    ([], "yet_to_reach_out"),
    ([call(disp="renewed")], "completed"),
    ([call(failed=True), call(disp="not_interested")], "completed"),
    ([call(failed=True), call(failed=True)], "in_progress_telephony_failed"),
    ([call(failed=True), call()], "in_progress_dialed"),
    ([call(connected=True, disp="callback_requested")], "in_progress_dialed"),
    # completed is checked before telephony_failed
    ([call(failed=True, disp="dnd")], "completed"),
])
def test_bucket(calls, bucket):
    assert lead_bucket(calls, TERMINAL) == bucket


def test_ratio_format():
    assert ratio_4dp(45, 80) == "0.5625"
    assert ratio_4dp(1, 3) == "0.3333"
    assert ratio_4dp(2, 3) == "0.6667"
    assert ratio_4dp(5, 5) == "1.0000"
