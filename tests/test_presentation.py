from decimal import Decimal
import pytest
from jevquant.presentation import format_confidence, format_latency_ms


@pytest.mark.parametrize(("value", "expected"), [
    (None, "—"),
    ("", "—"),
    ("NaN", "—"),
    (float("inf"), "—"),
    (-0.1, "—"),
    (1.1, "—"),
    (0, "0%"),
    (0.55, "55%"),
    (Decimal("0.555"), "56%"),
    (1, "100%"),
])
def test_confidence_never_invents_a_missing_score(value, expected):
    assert format_confidence(value) == expected


@pytest.mark.parametrize(("value", "expected"), [
    (None, "—"),
    ("", "—"),
    (0, "—"),
    (-1, "—"),
    ("nan", "—"),
    ("inf", "—"),
    (234, "234 ms"),
    (Decimal("12.5"), "13 ms"),
])
def test_latency_only_displays_valid_measurements(value, expected):
    assert format_latency_ms(value) == expected
