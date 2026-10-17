"""Deterministic backtest replay.

Honest scope: this engine replays a fixed, fully disclosed rule set over
*generated* (synthetic) candles so the operator can sanity-check the
plumbing - sizing, fees, stops, drawdown - without claiming predictive
value.  Every result carries ``is_synthetic`` and an ``assumptions`` list.
Real historical replay (Freqtrade/OHLCV archive) is a separate data source
that plugs into the same ``run`` entry point.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from ..domain.money import ZERO, canonical_str, quantize_down
from .features import Candle, atr, rsi, sma

BASE_PRICES = {
    "BTCUSDT": 68_000.0,
    "ETHUSDT": 3_200.0,
    "SOLUSDT": 170.0,
}
DEFAULT_BASE_PRICE = 100.0
QTY_STEP = Decimal("0.00001")


@dataclass(frozen=True)
class BacktestSpec:
    symbols: tuple[str, ...] = ("BTCUSDT",)
    minutes: int = 5
    candles: int = 1_500
    starting_equity_usdt: Decimal = Decimal("10000")
    max_position_notional_usdt: Decimal = Decimal("500")
    stop_loss_bps: Decimal = Decimal("120")
    take_profit_bps: Decimal = Decimal("240")
    max_holding_minutes: int = 240
    fee_bps: Decimal = Decimal("10")
    slippage_bps: Decimal = Decimal("2")
    seed: int = 42

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "BacktestSpec":
        def dec(key: str, default: Decimal) -> Decimal:
            value = payload.get(key)
            return default if value in (None, "") else Decimal(str(value))

        symbols = payload.get("symbols") or ["BTCUSDT"]
        return cls(
            symbols=tuple(str(symbol).upper() for symbol in symbols),
            minutes=int(payload.get("minutes", 5)),
            candles=int(payload.get("candles", 1_500)),
            starting_equity_usdt=dec("starting_equity_usdt", cls.starting_equity_usdt),
            max_position_notional_usdt=dec(
                "max_position_notional_usdt", cls.max_position_notional_usdt
            ),
            stop_loss_bps=dec("stop_loss_bps", cls.stop_loss_bps),
            take_profit_bps=dec("take_profit_bps", cls.take_profit_bps),
            max_holding_minutes=int(payload.get("max_holding_minutes", 240)),
            fee_bps=dec("fee_bps", cls.fee_bps),
            slippage_bps=dec("slippage_bps", cls.slippage_bps),
            seed=int(payload.get("seed", 42)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbols": list(self.symbols),
            "minutes": self.minutes,
            "candles": self.candles,
            "starting_equity_usdt": str(self.starting_equity_usdt),
            "max_position_notional_usdt": str(self.max_position_notional_usdt),
            "stop_loss_bps": str(self.stop_loss_bps),
            "take_profit_bps": str(self.take_profit_bps),
            "max_holding_minutes": self.max_holding_minutes,
            "fee_bps": str(self.fee_bps),
            "slippage_bps": str(self.slippage_bps),
            "seed": self.seed,
        }


@dataclass
class _OpenTrade:
    symbol: str
    qty: Decimal
    entry_price: Decimal
    entry_index: int
    entry_time: datetime
    entry_fee: Decimal = Decimal("0")


@dataclass
class _SymbolState:
    trades: list[dict[str, Any]] = field(default_factory=list)
    equity: Decimal = Decimal("0")
    peak: Decimal = Decimal("0")
    max_drawdown_pct: Decimal = Decimal("0")
    curve: list[dict[str, Any]] = field(default_factory=list)


def _generate_candles(spec: BacktestSpec, symbol: str) -> list[Candle]:
    rng = random.Random(f"{spec.seed}:{symbol}")
    price = BASE_PRICES.get(symbol, DEFAULT_BASE_PRICE)
    drift = 0.0
    vol = 0.0015
    start = datetime(2024, 1, 1, tzinfo=UTC)
    candles: list[Candle] = []
    for index in range(spec.candles):
        open_time = start + timedelta(minutes=spec.minutes * index)
        shock = rng.gauss(drift, vol)
        close = max(price * (1.0 + shock), price * 0.5)
        high = max(price, close) * (1.0 + abs(rng.gauss(0, vol / 2)))
        low = min(price, close) * (1.0 - abs(rng.gauss(0, vol / 2)))
        candles.append(
            Candle(
                open_time=open_time,
                close_time=open_time + timedelta(minutes=spec.minutes),
                open=price,
                high=high,
                low=low,
                close=close,
                volume=abs(rng.gauss(100, 25)),
            )
        )
        price = close
    return candles


def _close_trade(
    state: _SymbolState,
    trade: _OpenTrade,
    exit_price: Decimal,
    closed_at: datetime,
    holding_minutes: int,
    reason: str,
    fee_rate: Decimal,
) -> None:
    exit_fee = trade.qty * exit_price * fee_rate
    gross = trade.qty * (exit_price - trade.entry_price)
    # The entry fee already left the equity when the trade opened; the reported
    # per-trade PnL must still include it so net PnL matches the equity curve.
    state.equity += gross - exit_fee
    pnl = gross - trade.entry_fee - exit_fee
    state.trades.append(
        {
            "symbol": trade.symbol,
            "qty": canonical_str(trade.qty),
            "entry_price": canonical_str(trade.entry_price),
            "exit_price": canonical_str(exit_price),
            "opened_at": trade.entry_time.isoformat(),
            "closed_at": closed_at.isoformat(),
            "holding_minutes": holding_minutes,
            "pnl_usdt": canonical_str(pnl),
            "reason": reason,
        }
    )


def _run_symbol(spec: BacktestSpec, symbol: str) -> _SymbolState:
    candles = _generate_candles(spec, symbol)
    closes = [candle.close for candle in candles]
    state = _SymbolState(equity=spec.starting_equity_usdt, peak=spec.starting_equity_usdt)
    fee_rate = spec.fee_bps / Decimal("10000")
    slippage_rate = spec.slippage_bps / Decimal("10000")
    open_trade: _OpenTrade | None = None

    for index, candle in enumerate(candles):
        if index < 35:
            continue
        window = closes[: index + 1]
        fast = sma(window, 10)
        slow = sma(window, 30)
        momentum = rsi(window, 14)
        volatility = atr(candles[: index + 1], 14)
        if fast is None or slow is None or momentum is None or volatility is None:
            continue
        price = Decimal(str(candle.close))

        if open_trade is None:
            if fast > slow and momentum < 70.0:
                notional = min(
                    spec.max_position_notional_usdt, state.equity * Decimal("0.25")
                )
                if notional <= ZERO:
                    continue
                fill = price * (Decimal("1") + slippage_rate)
                qty = quantize_down(notional / fill, QTY_STEP)
                if qty <= ZERO:
                    continue
                entry_fee = qty * fill * fee_rate
                state.equity -= entry_fee
                open_trade = _OpenTrade(
                    symbol=symbol,
                    qty=qty,
                    entry_price=fill,
                    entry_index=index,
                    entry_time=candle.close_time,
                    entry_fee=entry_fee,
                )
        else:
            held = index - open_trade.entry_index
            stop_price = open_trade.entry_price * (
                Decimal("1") - spec.stop_loss_bps / Decimal("10000")
            )
            take_price = open_trade.entry_price * (
                Decimal("1") + spec.take_profit_bps / Decimal("10000")
            )
            reason = None
            if price <= stop_price:
                reason = "STOP_LOSS"
            elif price >= take_price:
                reason = "TAKE_PROFIT"
            elif held >= max(1, spec.max_holding_minutes // spec.minutes):
                reason = "MAX_HOLDING"
            elif fast < slow:
                reason = "TREND_REVERSAL"
            if reason is not None:
                exit_fill = price * (Decimal("1") - slippage_rate)
                _close_trade(
                    state,
                    open_trade,
                    exit_fill,
                    candle.close_time,
                    held * spec.minutes,
                    reason,
                    fee_rate,
                )
                open_trade = None

        state.peak = max(state.peak, state.equity)
        if state.peak > ZERO:
            drawdown = (state.peak - state.equity) / state.peak * Decimal("100")
            state.max_drawdown_pct = max(state.max_drawdown_pct, drawdown)
        state.curve.append(
            {
                "at": candle.close_time.isoformat(),
                "equity_usdt": canonical_str(state.equity),
            }
        )

    if open_trade is not None:
        last = candles[-1]
        exit_fill = Decimal(str(last.close)) * (Decimal("1") - slippage_rate)
        _close_trade(
            state,
            open_trade,
            exit_fill,
            last.close_time,
            (len(candles) - 1 - open_trade.entry_index) * spec.minutes,
            "END_OF_DATA",
            fee_rate,
        )
    return state


def run_backtest(spec: BacktestSpec) -> dict[str, Any]:
    """Run the replay and return a JSON-safe report."""

    per_symbol: dict[str, Any] = {}
    trades: list[dict[str, Any]] = []
    net_pnl = ZERO
    worst_drawdown = ZERO
    for symbol in spec.symbols:
        state = _run_symbol(spec, symbol)
        wins = [item for item in state.trades if Decimal(item["pnl_usdt"]) > ZERO]
        # The equity ledger is authoritative: per-trade PnL and the curve are
        # built from the same cash movements, so the net figure is the delta.
        pnl = state.equity - spec.starting_equity_usdt
        per_symbol[symbol] = {
            "trades": len(state.trades),
            "win_rate_pct": canonical_str(
                Decimal(len(wins)) / Decimal(len(state.trades)) * Decimal("100")
                if state.trades
                else ZERO
            ),
            "net_pnl_usdt": canonical_str(pnl),
            "ending_equity_usdt": canonical_str(state.equity),
            "return_pct": canonical_str(
                pnl / spec.starting_equity_usdt * Decimal("100")
                if spec.starting_equity_usdt > ZERO
                else ZERO
            ),
            "max_drawdown_pct": canonical_str(state.max_drawdown_pct),
            "equity_curve": state.curve[-400:],
        }
        trades.extend(state.trades)
        net_pnl += pnl
        worst_drawdown = max(worst_drawdown, state.max_drawdown_pct)

    total_trades = len(trades)
    total_wins = len([item for item in trades if Decimal(item["pnl_usdt"]) > ZERO])
    return {
        "engine": "deterministic_replay_v1",
        "is_synthetic": True,
        "generated_at": datetime.now(UTC).isoformat(),
        "spec": spec.as_dict(),
        "summary": {
            "trades": total_trades,
            "wins": total_wins,
            "losses": total_trades - total_wins,
            "win_rate_pct": canonical_str(
                Decimal(total_wins) / Decimal(total_trades) * Decimal("100")
                if total_trades
                else ZERO
            ),
            "net_pnl_usdt": canonical_str(net_pnl),
            "max_drawdown_pct": canonical_str(worst_drawdown),
            "symbols": list(spec.symbols),
        },
        "per_symbol": per_symbol,
        "trades": trades[-200:],
        "assumptions": [
            "Candles are generated from a seeded random walk, not real market data.",
            "Rule set is fixed: enter when SMA10 > SMA30 and RSI14 < 70; exit on "
            "stop, take profit, trend reversal or max holding.",
            "Fees and slippage are applied on both sides with Decimal arithmetic.",
            "Sizing uses min(max position notional, 25% of equity) and never "
            "re-derives quantity from float prices.",
            "Results are plumbing evidence only; they are not a performance claim.",
        ],
    }
