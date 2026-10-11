from decimal import Decimal
from typing import Any, Dict, List
import pandas as pd


def compute_indicators(candles: List[Dict[str, Any]]) -> Dict[str, Decimal]:
    """
    Computes technical indicators (SMA20, SMA60, RSI14, ATR14, Volatility)
    strictly from confirmed closed candles.
    
    Expects candles ordered from oldest to newest.
    Returns Decimal indicators for the most recently closed candle.
    """
    if len(candles) < 60:
        raise ValueError(f"Insufficient candles to compute indicators (need at least 60, got {len(candles)})")

    # Extract series as float for vector operations, convert back to Decimal at boundary
    closes = [float(c["close"]) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    volumes = [float(c["volume"]) for c in candles]

    df = pd.DataFrame({
        "close": closes,
        "high": highs,
        "low": lows,
        "volume": volumes
    })

    # 1. Moving Averages
    df["sma20"] = df["close"].rolling(window=20).mean()
    df["sma60"] = df["close"].rolling(window=60).mean()

    # 2. RSI (14 periods)
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=14, min_periods=14).mean()
    avg_loss = loss.rolling(window=14, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    df["rsi14"] = 100 - (100 / (1 + rs))
    # Fill edge cases where loss is 0
    df.loc[avg_loss == 0, "rsi14"] = 100.0

    # 3. ATR (14 periods)
    prev_close = df["close"].shift(1)
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"] - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(window=14).mean()

    # 4. Volatility (rolling 24-period standard deviation of pct returns)
    returns = df["close"].pct_change()
    df["volatility"] = returns.rolling(window=24).std()

    # 5. Price change 24h
    df["price_change_24h"] = df["close"].pct_change(periods=24)

    latest = df.iloc[-1]

    return {
        "sma20": Decimal(f"{latest['sma20']:.4f}"),
        "sma60": Decimal(f"{latest['sma60']:.4f}"),
        "rsi14": Decimal(f"{latest['rsi14']:.2f}"),
        "atr14": Decimal(f"{latest['atr14']:.4f}"),
        "volatility": Decimal(f"{(latest['volatility'] or 0.0):.6f}"),
        "price_change_24h": Decimal(f"{(latest['price_change_24h'] or 0.0):.4f}"),
    }
