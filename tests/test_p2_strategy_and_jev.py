from decimal import Decimal
from unittest.mock import MagicMock, patch
import pytest
from jevquant.indicators import compute_indicators
from jevquant.jev_engine import JevDecision, JevEngine
from jevquant.strategy import JevAIStrategy, SmaBaselineStrategy, make_signal_id


def generate_mock_candles(n=70, trend="up"):
    candles = []
    base_price = 50000.0
    for i in range(n):
        if trend == "up":
            price = base_price + i * 100.0
        elif trend == "down":
            price = base_price - i * 100.0
        else:
            price = base_price

        candles.append({
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "candle_close_utc": f"2026-10-11T{i:02d}:00:00Z",
            "open": Decimal(str(price - 10)),
            "high": Decimal(str(price + 20)),
            "low": Decimal(str(price - 20)),
            "close": Decimal(str(price)),
            "volume": Decimal("100.0"),
        })
    return candles


def test_compute_indicators_precision_and_values():
    candles = generate_mock_candles(n=70, trend="up")
    indicators = compute_indicators(candles)

    assert "sma20" in indicators
    assert "sma60" in indicators
    assert "rsi14" in indicators
    assert "atr14" in indicators
    assert "volatility" in indicators

    assert isinstance(indicators["sma20"], Decimal)
    assert isinstance(indicators["sma60"], Decimal)
    assert isinstance(indicators["rsi14"], Decimal)
    assert indicators["sma20"] > indicators["sma60"]
    assert Decimal("0") <= indicators["rsi14"] <= Decimal("100")


def test_compute_indicators_raises_when_insufficient_data():
    candles = generate_mock_candles(n=40)
    with pytest.raises(ValueError, match="Insufficient candles"):
        compute_indicators(candles)


def test_sma_baseline_golden_cross_and_death_cross():
    strategy = SmaBaselineStrategy()

    # Golden cross: SMA20 > SMA60, no position -> BUY
    sig_buy = strategy.evaluate(
        symbol="BTC/USDT",
        candle_close_utc="2026-10-11T12:00:00Z",
        indicators={"sma20": Decimal("62000.0"), "sma60": Decimal("60000.0")},
        has_position=False,
    )
    assert sig_buy.action == "BUY"
    assert sig_buy.reason_code == "SMA20_ABOVE_SMA60_GOLDEN_CROSS"

    # Death cross: SMA20 < SMA60, has position -> SELL
    sig_sell = strategy.evaluate(
        symbol="BTC/USDT",
        candle_close_utc="2026-10-11T13:00:00Z",
        indicators={"sma20": Decimal("59000.0"), "sma60": Decimal("60000.0")},
        has_position=True,
    )
    assert sig_sell.action == "SELL"
    assert sig_sell.reason_code == "SMA20_BELOW_SMA60_DEATH_CROSS"


def test_signal_id_determinism_per_candle_period():
    sig1 = make_signal_id("BTC/USDT", "2026-10-11T12:00:00Z", "sma_baseline", "v1.0")
    sig2 = make_signal_id("BTC/USDT", "2026-10-11T12:00:00Z", "sma_baseline", "v1.0")
    sig3 = make_signal_id("BTC/USDT", "2026-10-11T13:00:00Z", "sma_baseline", "v1.0")

    assert sig1 == sig2
    assert sig1 != sig3


def test_jev_engine_fallback_to_hold_when_no_api_key():
    engine = JevEngine(api_key=None)
    decision = engine.decide({"close": 60000, "sma20": 60000})

    assert decision.action == "HOLD"
    assert decision.reason_code == "NO_API_KEY_FALLBACK_HOLD"


def test_jev_engine_mock_successful_call():
    mock_engine = MagicMock(spec=JevEngine)
    mock_engine.decide.return_value = JevDecision(
        action="BUY",
        confidence=0.88,
        latency_ms=234,
        model_name="jev-latest",
        reason_code="JEV_DECISION_SUCCESS",
        raw_response={"choices": {"action": "BUY"}},
        timestamp_utc="2026-10-11T12:00:00Z",
    )

    strategy = JevAIStrategy(jev_engine=mock_engine)
    sig = strategy.evaluate(
        symbol="BTC/USDT",
        candle_close_utc="2026-10-11T12:00:00Z",
        indicators={"sma20": Decimal("61000"), "sma60": Decimal("60000"), "rsi14": Decimal("65"), "atr14": Decimal("450")},
        current_close=Decimal("61500"),
        has_position=False,
    )

    assert sig.action == "BUY"
    assert sig.reason_code == "JEV_DECISION_SUCCESS"
    assert sig.metadata["latency_ms"] == 234
    assert sig.metadata["confidence"] == 0.88


def test_jev_engine_fallback_on_network_or_api_exception():
    with patch("typesafe_sdk.TypeSafeClient") as MockClient:
        instance = MockClient.return_value.__enter__.return_value
        instance.system_one.side_effect = TimeoutError("Connection to Jev timed out")

        engine = JevEngine(api_key="mock_key_for_test")
        decision = engine.decide({"close": 65000})

        # Fails closed
        assert decision.action == "HOLD"
        assert "JEV_ERROR_FALLBACK_HOLD" in decision.reason_code


@pytest.mark.parametrize(("confidence", "expected_action", "expected_reason"), [
    (0.55, "BUY", "JEV_DECISION_SUCCESS"),
    (0.54, "HOLD", "JEV_LOW_CONFIDENCE_HOLD"),
    (None, "HOLD", "JEV_INVALID_CONFIDENCE_HOLD"),
    (float("nan"), "HOLD", "JEV_INVALID_CONFIDENCE_HOLD"),
    (float("inf"), "HOLD", "JEV_INVALID_CONFIDENCE_HOLD"),
    (-0.1, "HOLD", "JEV_INVALID_CONFIDENCE_HOLD"),
    (1.1, "HOLD", "JEV_INVALID_CONFIDENCE_HOLD"),
])
def test_jev_strategy_confidence_gate(confidence, expected_action, expected_reason):
    engine = MagicMock(spec=JevEngine)
    engine.decide.return_value = JevDecision(
        action="BUY", confidence=confidence, latency_ms=12, model_name="jev-latest",
        reason_code="JEV_DECISION_SUCCESS", timestamp_utc="2026-10-11T12:00:00Z",
    )
    strategy = JevAIStrategy(jev_engine=engine)
    result = strategy.evaluate(
        symbol="BTC/USDT", candle_close_utc="2026-10-11T12:00:00Z",
        indicators={
            "sma20": Decimal("62000"), "sma60": Decimal("61000"),
            "rsi14": Decimal("60"), "atr14": Decimal("400"),
        },
        current_close=Decimal("62000"),
    )
    assert result.action == expected_action
    assert result.reason_code == expected_reason
    assert result.metadata["raw_action"] == "BUY"


def test_jev_confidence_gate_preserves_hold_and_validates_configuration():
    strategy = JevAIStrategy(min_confidence=Decimal("0.8"))
    assert strategy.min_confidence == Decimal("0.8")
    for invalid in (Decimal("-0.01"), Decimal("1.01"), Decimal("NaN")):
        with pytest.raises(ValueError, match="min_confidence"):
            JevAIStrategy(min_confidence=invalid)
