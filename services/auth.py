"""Minimal app auth — pbkdf2 password hashing + HMAC-signed tokens, stdlib only.

No signup: users are added by inserting a row into the `app_users` table. Hash a
password for the INSERT with:

    python3 -m services.auth hash 'the-password'
    # → pbkdf2$200000$<salt>$<hash>
    # INSERT INTO app_users(username, password_hash) VALUES ('alice', '<that>');

# ponytail: pbkdf2+HMAC over adding bcrypt/PyJWT — stdlib covers a single-tenant
# internal login. Swap to bcrypt/argon2 if this ever faces the open internet.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time

from fastapi import Header, HTTPException

from config import JWT_SECRET, AUTH_TOKEN_TTL

PBKDF2_ROUNDS = 200_000


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2${PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(username: str) -> str:
    payload = {"sub": username, "exp": int(time.time()) + AUTH_TOKEN_TTL}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(JWT_SECRET.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(sig)}"


def verify_token(token: str) -> str | None:
    try:
        body, sig = token.split(".")
        expected = hmac.new(JWT_SECRET.encode(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64(expected), sig):
            return None
        payload = json.loads(_b64d(body))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload.get("sub")
    except Exception:
        return None


async def require_auth(authorization: str = Header(None)) -> str:
    """FastAPI dependency — 401 unless a valid Bearer token is present."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Not authenticated")
    user = verify_token(authorization.split(" ", 1)[1].strip())
    if not user:
        raise HTTPException(401, "Invalid or expired token")
    return user


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 3 and sys.argv[1] == "hash":
        print(hash_password(sys.argv[2]))
    elif len(sys.argv) == 4 and sys.argv[1] == "check":   # check <pw> <stored>
        print(verify_password(sys.argv[2], sys.argv[3]))
    else:
        # ponytail self-check: round-trip a hash and a token.
        h = hash_password("s3cret")
        assert verify_password("s3cret", h) and not verify_password("nope", h)
        assert verify_token(make_token("alice")) == "alice"
        assert verify_token("garbage.sig") is None
        print("✅ auth self-check passed. Usage: python3 -m services.auth hash '<password>'")