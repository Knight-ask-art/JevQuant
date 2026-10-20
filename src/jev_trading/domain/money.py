"""Decimal-only money/quantity helpers.

No binary float is ever used to size an order, compute a notional, or
round a quantity. Floats are accepted only for non-economic inputs
(probabilities, scores) and must be converted explicitly.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")


class MoneyError(ValueError):
    pass


def dec(value: Decimal | int | str | float) -> Decimal:
    """Convert to Decimal without inheriting binary float noise where possible."""
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, (int, str)):
        result = Decimal(value)
    elif isinstance(value, float):
        result = Decimal(str(value))
    else:
        raise MoneyError(f"unsupported numeric type: {type(value)!r}")
    if not result.is_finite():
        raise MoneyError("non-finite decimal is not allowed")
    return result


def bps_to_rate(bps: Decimal | int | str) -> Decimal:
    return dec(bps) / BPS


def apply_bps(price: Decimal, bps: Decimal | int | str) -> Decimal:
    return price * (ONE + bps_to_rate(bps))


def pct_to_rate(pct: Decimal | int | str) -> Decimal:
    return dec(pct) / Decimal("100")


def quantize_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= ZERO:
        raise MoneyError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def quantize_nearest(value: Decimal, step: Decimal) -> Decimal:
    if step <= ZERO:
        raise MoneyError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_HALF_UP) * step


def positive(value: Decimal, field: str) -> Decimal:
    if value <= ZERO:
        raise MoneyError(f"{field} must be positive")
    return value


def non_negative(value: Decimal, field: str) -> Decimal:
    if value < ZERO:
        raise MoneyError(f"{field} must be non-negative")
    return value


def canonical_str(value: Decimal) -> str:
    """Stable decimal representation for hashing and persistence."""
    normalized = value.normalize()
    if normalized == ZERO:
        return "0"
    return format(normalized, "f")


def safe_div(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == ZERO:
        raise MoneyError("division by zero")
    return numerator / denominator


def round_price(price: Decimal, tick: Decimal) -> Decimal:
    """Round a price down to the exchange tick.

    ``tick`` is a step, not a precision: Binance reports ``tickSize`` as
    ``"0.01000000"`` (eight decimals), whose *exponent* would let
    ``Decimal.quantize`` through a price of ``81777.56224200``. Dividing by
    the tick value makes the step independent of how it is written.
    """
    if tick <= ZERO:
        raise MoneyError("tick must be positive")
    try:
        return max(quantize_down(price, tick), ZERO)
    except InvalidOperation as exc:  # pragma: no cover - defensive
        raise MoneyError("invalid price precision") from exc
