"""Reversible encryption for stored secrets (SAP account passwords).

Fernet = AES-128-CBC + HMAC-SHA256. We derive the Fernet key from ENCRYPTION_KEY
(sha256 → urlsafe-base64) so any existing .env secret works without reformatting.

# ponytail: decrypt() passes non-tokens through unchanged, so plaintext rows keep
# working during the one-time migration. Remove the fallback once all rows are encrypted.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from config import ENCRYPTION_KEY


def _fernet() -> Fernet:
    if not ENCRYPTION_KEY:
        raise RuntimeError("ENCRYPTION_KEY not set — cannot encrypt/decrypt credentials")
    key = base64.urlsafe_b64encode(hashlib.sha256(ENCRYPTION_KEY.encode()).digest())
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt((plaintext or "").encode()).decode()


def decrypt(value: str) -> str:
    """Decrypt a Fernet token; return the value unchanged if it isn't one (legacy plaintext)."""
    if not value:
        return value
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        return value


def is_encrypted(value: str) -> bool:
    if not value:
        return False
    try:
        _fernet().decrypt(value.encode())
        return True
    except InvalidToken:
        return False


if __name__ == "__main__":
    # ponytail self-check: round-trip + plaintext passthrough.
    ct = encrypt("hunter2")
    assert decrypt(ct) == "hunter2", "roundtrip failed"
    assert ct != "hunter2" and is_encrypted(ct), "should be a token"
    assert decrypt("legacy-plaintext") == "legacy-plaintext", "plaintext must pass through"
    assert not is_encrypted("legacy-plaintext")
    print("✅ crypto self-check passed")
