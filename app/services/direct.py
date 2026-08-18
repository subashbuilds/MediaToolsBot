from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from pathlib import Path


def make_token(secret: str, path: Path, ttl: int) -> tuple[str, int]:
    expires = int(time.time()) + max(60, ttl)
    nonce = secrets.token_urlsafe(12)
    raw = f"{path.resolve()}|{expires}|{nonce}".encode()
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()[:32]
    return f"{expires}.{nonce}.{sig}", expires


def verify_token(secret: str, path: Path, token: str) -> bool:
    try:
        expires_s, nonce, sig = token.split(".", 2)
        expires = int(expires_s)
    except (ValueError, TypeError):
        return False
    if expires < int(time.time()):
        return False
    raw = f"{path.resolve()}|{expires}|{nonce}".encode()
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(expected, sig)
