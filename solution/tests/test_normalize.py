from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from ledger.normalize import (normalise_header, normalise_phone, parse_date, parse_decimal,
                              parse_enum, parse_timestamp)


@pytest.mark.parametrize("raw", [
    "9876543210", "+91 98765 43210", "09876543210", "91-98765-43210", "(+91) 9876543210",
    "+919876543210", " 98765-43210 ",
])
def test_phone_all_six_formats(raw):
    assert normalise_phone(raw) == "+919876543210"


@pytest.mark.parametrize("raw", [
    "987654321",          # 9 digits
    "5876543210",         # bad first digit
    "022-2345-6789",      # landline
    "N/A", "", "+1 415 555 0134",
    "9198765432101",      # 13 digits
    "0919876543210",      # 13 digits
    "915876543210",       # 91 + bad first digit
])
def test_phone_invalid(raw):
    assert normalise_phone(raw) is None


def test_phone_12_digits_must_start_with_91():
    assert normalise_phone("929876543210") is None


@pytest.mark.parametrize("raw,expected", [
    ("2026-12-01", date(2026, 12, 1)),
    ("01/12/2026", date(2026, 12, 1)),      # day first!
    ("01-12-2026", date(2026, 12, 1)),
    ("01-Dec-2026", date(2026, 12, 1)),
    ("01-dec-2026", date(2026, 12, 1)),
    ("29/02/2028", date(2028, 2, 29)),      # leap year
])
def test_date_valid(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", [
    "31/02/2026", "2026-13-01", "29/02/2026", "soon", "TBD", "12/31/2026", "01/12-2026", "01-Dez-2026", "",
])
def test_date_invalid(raw):
    assert parse_date(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("27554", "27554.00"), ("₹27,554", "27554.00"), ("Rs. 24787", "24787.00"), ("rs 5", "5.00"),
    ("Rs.5", "5.00"), ("INR 25301.4", "25301.40"), ("inr25301.456", "25301.46"), ("0", "0.00"),
    ("1,23,456.785", "123456.79"),
])
def test_decimal_valid(raw, expected):
    assert parse_decimal(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["N/A", "-", "twelve thousand", "-100", "₹-5", "USD 5", "", "1.2.3"])
def test_decimal_invalid(raw):
    assert parse_decimal(raw) is None


def test_enum():
    assert parse_enum(" HI ", ["en", "hi"]) == "hi"
    assert parse_enum("Hindi", ["en", "hi"]) is None


@pytest.mark.parametrize("h,expected", [
    ("Mobile No", "mobile no"), ("  mobile__no ", "mobile no"), ("MOBILE \t _ NO", "mobile no"),
    ("customer_name", "customer name"),
])
def test_header(h, expected):
    assert normalise_header(h) == expected


def test_timestamps_three_forms_same_instant():
    utc = datetime(2026, 10, 3, 4, 31, tzinfo=timezone.utc)
    assert parse_timestamp("2026-10-03T04:31:00Z") == utc
    assert parse_timestamp("2026-10-03T10:01:00+05:30") == utc
    assert parse_timestamp("2026-10-03 10:01:00") == utc     # naive means IST
    assert parse_timestamp("garbage") is None
    assert parse_timestamp(None) is None
