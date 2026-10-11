from datetime import datetime, timezone
from decimal import Decimal
import hashlib
from typing import Any, Dict, Optional
import pydantic
from jevquant.jev_engine import JevDecision, JevEngine


class SignalOutput(pydantic.BaseModel):
    signal_id: str
    symbol: str
    candle_close_utc: str
    strategy_name: str
    strategy_version: str
    action: str  # BUY, SELL, HOLD
    reason_code: str
    metadata: Dict[str, Any]
    created_at_utc: str


def make_signal_id(symbol: str, candle_close_utc: str, strategy_name: str, strategy_version: str) -> str:
    """
    Generates a deterministic unique signal ID per closed candle and strategy.
    Prevents duplicate signal generation for the same market period.
    """
    raw = f"{symbol}:{candle_close_utc}:{strategy_name}:{strategy_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class SmaBaselineStrategy:
    """
    Simple deterministic SMA20 / SMA60 baseline comparison strategy.
    Does not require any AI SDK or external API keys.
    """
    def __init__(self, name: str = "sma_baseline", version: str = "v1.0"):
        self.name = name
        self.version = version

    def evaluate(
        self,
        symbol: str,
        candle_close_utc: str,
        indicators: Dict[str, Decimal],
        has_position: bool = False,
    ) -> SignalOutput:
        sma20 = indicators["sma20"]
        sma60 = indicators["sma60"]
        now_utc = datetime.now(timezone.utc).isoformat()
        sig_id = make_signal_id(symbol, candle_close_utc, self.name, self.version)

        if not has_position and sma20 > sma60:
            action = "BUY"
            reason = "SMA20_ABOVE_SMA60_GOLDEN_CROSS"
        elif has_position and sma20 < sma60:
            action = "SELL"
            reason = "SMA20_BELOW_SMA60_DEATH_CROSS"
        else:
            action = "HOLD"
            reason = "SMA_NO_TRANSITION"

        return SignalOutput(
            signal_id=sig_id,
            symbol=symbol,
            candle_close_utc=candle_close_utc,
            strategy_name=self.name,
            strategy_version=self.version,
            action=action,
            reason_code=reason,
            metadata={
                "sma20": str(sma20),
                "sma60": str(sma60),
                "has_position": has_position,
            },
            created_at_utc=now_utc,
        )


class JevAIStrategy:
    """
    AI-driven strategy querying TypeSafe Jev via JevEngine.
    Safe fallbacks to HOLD when the engine is unavailable or unconfigured.
    """
    def __init__(
        self,
        jev_engine: Optional[JevEngine] = None,
        name: str = "jev_system_one",
        version: str = "v1.0",
    ):
        self.engine = jev_engine or JevEngine()
        self.name = name
        self.version = version

    def evaluate(
        self,
        symbol: str,
        candle_close_utc: str,
        indicators: Dict[str, Decimal],
        current_close: Decimal,
        has_position: bool = False,
    ) -> SignalOutput:
        sig_id = make_signal_id(symbol, candle_close_utc, self.name, self.version)
        now_utc = datetime.now(timezone.utc).isoformat()

        market_state = {
            "symbol": symbol,
            "candle_close_utc": candle_close_utc,
            "close": current_close,
            "sma20": indicators["sma20"],
            "sma60": indicators["sma60"],
            "rsi14": indicators["rsi14"],
            "atr14": indicators["atr14"],
            "volatility": indicators.get("volatility", Decimal("0")),
            "price_change_24h": indicators.get("price_change_24h", Decimal("0")),
            "has_position": has_position,
        }

        decision: JevDecision = self.engine.decide(market_state)

        return SignalOutput(
            signal_id=sig_id,
            symbol=symbol,
            candle_close_utc=candle_close_utc,
            strategy_name=self.name,
            strategy_version=self.version,
            action=decision.action,
            reason_code=decision.reason_code,
            metadata={
                "confidence": decision.confidence,
                "latency_ms": decision.latency_ms,
                "model_name": decision.model_name,
                "market_state": {k: str(v) for k, v in market_state.items()},
            },
            created_at_utc=now_utc,
        )
