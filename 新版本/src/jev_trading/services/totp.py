"""RFC 6238 TOTP verification for the Live arming handshake."""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time


def _hotp(secret: bytes, counter: int, digits: int = 6) -> str:
    digest = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**digits)).zfill(digits)


def verify_totp(secret_b32: str, code: str, *, now: float | None = None, window: int = 1) -> bool:
    if not secret_b32 or not code:
        return False
    try:
        secret = base64.b32decode(secret_b32.strip().upper(), casefold=True)
    except Exception:  # noqa: BLE001
        return False
    current = int((now or time.time()) // 30)
    supplied = str(code).strip()
    for offset in range(-window, window + 1):
        if hmac.compare_digest(_hotp(secret, current + offset), supplied):
            return True
    return False
