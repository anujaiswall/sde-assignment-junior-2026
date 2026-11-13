"""PII protection at rest.

Two different tools, for two different needs:

* Phone numbers must be *matched* (an event's phone -> the lead's phone), so we
  store a keyed hash: HMAC-SHA256(secret, '+91XXXXXXXXXX'). The same phone
  always gives the same hash, so equality joins work in SQL, but without the
  secret you cannot reverse it or brute-force the ~4 billion possible numbers.
  (A plain SHA-256 would be brute-forceable in minutes.)
* Other PII (customer_name, and the phone itself for recovery) only needs to be
  *read back*, never searched, so it is encrypted with Fernet (AES + HMAC).

The secret comes from the LEDGER_SECRET environment variable. If it is not set
we create one in a local `.env` file (which the repo's .gitignore already
excludes). In production it would come from a secrets manager / KMS.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

ENV_VAR = "LEDGER_SECRET"


def load_or_create_secret(env_file: Path = Path(".env")) -> bytes:
    value = os.environ.get(ENV_VAR)
    if not value and env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith(ENV_VAR + "="):
                value = line.split("=", 1)[1].strip()
    if not value:
        value = secrets.token_urlsafe(32)
        with env_file.open("a") as fh:
            fh.write(f"{ENV_VAR}={value}\n")
    return value.encode()


class PiiVault:
    def __init__(self, secret: bytes):
        # Derive two independent keys from one secret: one for hashing, one for encryption.
        self._hash_key = hashlib.sha256(b"hash:" + secret).digest()
        enc_key = hashlib.sha256(b"enc:" + secret).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(enc_key))

    def phone_hash(self, canonical_phone: str) -> str:
        return hmac.new(self._hash_key, canonical_phone.encode(), hashlib.sha256).hexdigest()

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()
