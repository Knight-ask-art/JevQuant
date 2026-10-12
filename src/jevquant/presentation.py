"""Format model diagnostics without inventing missing measurements."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


def _bounded_decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation):
        return None
    return result if result.is_finite() else None


def format_confidence(value: Any) -> str:
    """0..1 confidence as percentage; an absent/invalid value is unknown."""
    if value is None:
        return "—"
    score = _bounded_decimal(value)
    if score is None or not Decimal("0") <= score <= Decimal("1"):
        return "—"
    rounded = (score * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{rounded}%"


def format_latency_ms(value: Any) -> str:
    """Display measured positive latency only; zero is not an API response."""
    if value is None:
        return "—"
    latency = _bounded_decimal(value)
    if latency is None or latency <= 0:
        return "—"
    ms = latency.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{ms} ms"
