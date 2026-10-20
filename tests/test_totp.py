"""RFC 6238 TOTP verification used by the live arming handshake."""

from jev_trading.services.totp import verify_totp

# Base32 of the RFC 6238 SHA-1 seed "12345678901234567890".
SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def test_rfc6238_vector_at_t59_returns_287082():
    assert verify_totp(SECRET, "287082", now=59.0) is True


def test_wrong_code_is_rejected():
    assert verify_totp(SECRET, "000000", now=59.0) is False


def test_window_accepts_previous_step():
    # Counter 1 code (287082) is still accepted one step later.
    assert verify_totp(SECRET, "287082", now=89.0) is True


def test_invalid_inputs_are_rejected():
    assert verify_totp("", "287082", now=59.0) is False
    assert verify_totp(SECRET, "", now=59.0) is False
    assert verify_totp("not-base32!!", "287082", now=59.0) is False
