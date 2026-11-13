"""Pure functions that turn messy raw values into clean, canonical ones.

Every function here takes a raw string and returns either the clean value or
None when the value is invalid. None of them touch the database, so they are
easy to unit-test (see tests/test_normalize.py).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------- headers

_SEP_RUN = re.compile(r"[_\s]+")


def normalise_header(h: str) -> str:
    """'  Mobile_No ' -> 'mobile no'. Lower-case, trim, collapse runs of _ / whitespace."""
    return _SEP_RUN.sub(" ", h.strip().lower()).strip()


# ---------------------------------------------------------------- phone

def normalise_phone(raw: str | None) -> str | None:
    """Return '+91XXXXXXXXXX' for a valid Indian mobile, else None."""
    if raw is None:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


# ---------------------------------------------------------------- date

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DMY = re.compile(r"^(\d{1,2})([/-])(\d{1,2})\2(\d{4})$")  # DD/MM/YYYY or DD-MM-YYYY (same separator)
_DMONY = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")   # DD-Mon-YYYY


def parse_date(raw: str | None) -> date | None:
    """Accept YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD-Mon-YYYY. Day-first. None if invalid."""
    if raw is None:
        return None
    s = raw.strip()
    try:
        if m := _ISO.match(s):
            y, mo, d = int(m[1]), int(m[2]), int(m[3])
        elif m := _DMY.match(s):
            d, mo, y = int(m[1]), int(m[3]), int(m[4])
        elif m := _DMONY.match(s):
            d, y = int(m[1]), int(m[3])
            mo = _MONTHS.get(m[2].lower())
            if mo is None:
                return None
        else:
            return None
        return date(y, mo, d)  # raises ValueError for 31/02, month 13, ...
    except ValueError:
        return None


# ---------------------------------------------------------------- decimal

_CURRENCY = re.compile(r"^(?:₹|rs\.?|inr)?\s*", re.IGNORECASE)
_NUMBER = re.compile(r"^\d[\d,]*(?:\.\d+)?$|^\.\d+$")


def parse_decimal(raw: str | None) -> Decimal | None:
    """'₹27,554' / 'Rs. 100' / 'INR 25301.4' -> Decimal rounded to 2 places. None if invalid or < 0."""
    if raw is None:
        return None
    s = _CURRENCY.sub("", raw.strip(), count=1)
    if not _NUMBER.match(s):   # also rejects '-5', 'N/A', 'twelve thousand'
        return None
    try:
        value = Decimal(s.replace(",", ""))
    except InvalidOperation:
        return None
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------- enum

def parse_enum(raw: str | None, allowed: list[str]) -> str | None:
    if raw is None:
        return None
    v = raw.strip().lower()
    return v if v in {a.lower() for a in allowed} else None


# ---------------------------------------------------------------- timestamps

def parse_timestamp(raw) -> datetime | None:
    """Parse an event timestamp to an aware UTC datetime.

    '...Z' = UTC, '...+05:30' = that offset, no offset at all = IST (per spec).
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(timezone.utc)


def to_utc_key(dt: datetime) -> str:
    """Fixed-width UTC string so that text sorting == time sorting in SQL."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def from_utc_key(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
