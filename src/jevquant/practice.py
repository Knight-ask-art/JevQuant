"""Local-only beginner paper-trading practice with synthetic candles.

This module has no market-data client, API credentials, order adapter or
persistent storage. It uses the existing paper ledger and risk checker.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from jevquant.orders import PaperAccount, PaperFill
from jevquant.risk import RiskManager


# Intentionally fictional prices: never label these as historical/exchange quotes.
DEMO_PRICES: tuple[Decimal, ...] = tuple(Decimal(value) for value in (
    "60000", "60700", "61400", "59300", "58500", "61800",
    "62700", "60600", "57400", "59500", "61300", "63000",
))


def demo_market_rules() -> dict:
    """Illustrative limits for training only; NOT live Binance exchangeInfo."""
    return {
        "symbol": "BTC/USDT",
        "status": "TRADING",
        "min_notional": Decimal("5"),
        "min_qty": Decimal("0.00001"),
        "max_qty": Decimal("9000"),
        "step_size": Decimal("0.00001"),
        "min_price": Decimal("0.01"),
        "max_price": Decimal("1000000"),
        "tick_size": Decimal("0.01"),
    }


@dataclass(frozen=True)
class PracticeEvent:
    candle_number: int
    action: str
    approved: bool
    reason: str
    price: Decimal
    quantity: Decimal = Decimal("0")
    fee_usdt: Decimal = Decimal("0")


class PaperPractice:
    """An isolated, per-session tutorial; actions are initiated by button clicks."""

    def __init__(
        self,
        initial_usdt: Decimal = Decimal("7.00"),
        prices: tuple[Decimal, ...] = DEMO_PRICES,
    ) -> None:
        if not initial_usdt.is_finite() or initial_usdt <= 0:
            raise ValueError("initial_usdt must be finite and positive")
        if not prices or any(not p.is_finite() or p <= 0 for p in prices):
            raise ValueError("prices must be nonempty, finite and positive")
        self.prices = tuple(prices)
        self.cursor = 0
        self.account = PaperAccount(initial_usdt=initial_usdt)
        # This tutorial permits several entries for learning. It is not a live
        # trading risk policy and has no exchange privileges.
        self.risk = RiskManager(initial_capital_usdt=initial_usdt, max_daily_entries=3)
        self.events: list[PracticeEvent] = []

    @property
    def price(self) -> Decimal:
        return self.prices[self.cursor]

    @property
    def candle_number(self) -> int:
        return self.cursor + 1

    @property
    def completed(self) -> bool:
        return self.cursor == len(self.prices) - 1

    @property
    def equity(self) -> Decimal:
        return self.account.get_equity(self.price)

    @property
    def pnl(self) -> Decimal:
        return self.equity - self.account.initial_usdt

    def _record(
        self, action: str, approved: bool, reason: str,
        fill: Optional[PaperFill] = None,
    ) -> PracticeEvent:
        event = PracticeEvent(
            candle_number=self.candle_number,
            action=action,
            approved=approved,
            reason=reason,
            price=self.price,
            quantity=fill.qty if fill else Decimal("0"),
            fee_usdt=fill.fee_usdt if fill else Decimal("0"),
        )
        self.events.append(event)
        return event

    def next_candle(self) -> PracticeEvent:
        if self.completed:
            return self._record("NEXT", False, "SCENARIO_COMPLETE")
        self.cursor += 1
        return self._record("NEXT", True, "SYNTHETIC_CANDLE_ADVANCED")

    def buy(self) -> PracticeEvent:
        """Risk-checked training buy, never a real exchange order."""
        result = self.risk.evaluate_entry(
            signal_action="BUY",
            current_price=self.price,
            market_rules=demo_market_rules(),
            available_usdt=self.account.usdt_balance,
            has_position=self.account.has_position,
            today_realized_loss_usdt=self.account.today_realized_loss_usdt,
        )
        if not result.approved:
            return self._record("BUY", False, result.reason_code)
        try:
            fill = self.account.execute_buy(
                symbol="BTC/USDT",
                target_qty=result.target_quantity,
                market_price=self.price,
                client_order_id=f"practice-buy-{len(self.events) + 1}",
            )
        except (ValueError, RuntimeError, ArithmeticError):
            return self._record("BUY", False, "PAPER_EXECUTION_REJECTED")
        self.risk.record_entry_executed()
        return self._record("BUY", True, "PAPER_BUY_FILLED", fill)

    def sell(self) -> PracticeEvent:
        """Close an existing simulated position, respecting demo min notional."""
        if not self.account.has_position:
            return self._record("SELL", False, "NO_PAPER_POSITION")
        qty = self.account.btc_balance
        rules = demo_market_rules()
        expected_execution_price = self.price * (Decimal("1") - self.account.slippage_pct)
        if qty < rules["min_qty"] or qty * expected_execution_price < rules["min_notional"]:
            return self._record("SELL", False, "DEMO_EXIT_BELOW_MIN_NOTIONAL")
        try:
            fill = self.account.execute_sell(
                symbol="BTC/USDT",
                sell_qty=qty,
                market_price=self.price,
                client_order_id=f"practice-sell-{len(self.events) + 1}",
            )
        except (ValueError, RuntimeError, ArithmeticError):
            return self._record("SELL", False, "PAPER_EXECUTION_REJECTED")
        return self._record("SELL", True, "PAPER_SELL_FILLED", fill)
