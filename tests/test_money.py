"""Decimal-only money helpers."""

from decimal import Decimal

import pytest

from jev_trading.domain.money import (
    apply_bps,
    bps_to_rate,
    canonical_str,
    dec,
    quantize_down,
    quantize_nearest,
    round_price,
    safe_div,
)


def test_dec_converts_without_float_noise():
    assert dec("0.1") == Decimal("0.1")
    assert dec(Decimal("2")) == Decimal("2")
    assert dec(0.1) == Decimal("0.1")


def test_canonical_str_is_plain_and_stable():
    assert canonical_str(Decimal("1.230000")) == "1.23"
    assert canonical_str(Decimal("100")) == "100"
    assert canonical_str(Decimal("-0.000")) == "0"


def test_quantize_down_never_rounds_up():
    assert quantize_down(Decimal("1.239"), Decimal("0.01")) == Decimal("1.23")
    assert quantize_down(Decimal("-1.239"), Decimal("0.01")) == Decimal("-1.23")


def test_quantize_nearest_handles_negative_values():
    assert quantize_nearest(Decimal("1.235"), Decimal("0.01")) == Decimal("1.24")
    assert quantize_nearest(Decimal("-1.235"), Decimal("0.01")) == Decimal("-1.24")


def test_round_price_uses_the_tick_value_not_its_precision():
    # Binance serializes a 0.01 tick as "0.01000000"; quantize() would treat
    # that as an eight-decimal precision and let a 6-decimal price through.
    tick = Decimal("0.01000000")
    assert round_price(Decimal("81777.56224200"), tick) == Decimal("81777.56")
    assert round_price(Decimal("81777.56"), tick) == Decimal("81777.56")
    assert round_price(Decimal("0.0000123"), Decimal("0.00000100")) == Decimal("0.000012")


def test_round_price_rejects_non_positive_tick():
    with pytest.raises(ValueError):
        round_price(Decimal("1"), Decimal("0"))


def test_bps_helpers():
    assert bps_to_rate(Decimal("10")) == Decimal("0.001")
    assert apply_bps(Decimal("100"), Decimal("10")) == Decimal("100.1")


def test_safe_div_rejects_zero_denominator():
    with pytest.raises(ValueError):
        safe_div(Decimal("1"), Decimal("0"))


def test_quantize_rejects_non_positive_step():
    with pytest.raises(ValueError):
        quantize_down(Decimal("1"), Decimal("0"))
