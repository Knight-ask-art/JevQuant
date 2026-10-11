from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional
import pydantic


class RiskCheckResult(pydantic.BaseModel):
    approved: bool
    reason_code: str
    target_quantity: Decimal = Decimal("0")
    estimated_notional: Decimal = Decimal("0")
    checks_detail: Dict[str, Any]
    evaluated_at_utc: str


class RiskManager:
    """
    Independent Risk Manager for JevQuant.
    Core invariants:
    1. Capital cap: total budget <= initial_capital_usdt (e.g. 50 CNY ~ 7.00 USDT).
    2. Max open positions: 1 (single-position spot only).
    3. Daily new entries: max 1 per UTC day.
    4. Exit feasibility: before buying, verify that if price drops by reference_drawdown (3%)
       minus entry/exit fees (0.1% each) and slippage (0.05%), the resulting position
       still meets exchange min_notional. If not, reject buy (REJECT_EXIT_MIN_NOTIONAL).
    5. Daily loss halt: if today's equity loss >= 3% of initial capital, halt new buys.
    6. Circuit breakers: manual halt, missing rules, stale feeds.
    """
    def __init__(
        self,
        initial_capital_usdt: Decimal = Decimal("7.00"),
        max_daily_entries: int = 1,
        daily_loss_halt_pct: Decimal = Decimal("0.03"),
        fee_rate: Decimal = Decimal("0.001"),  # 0.1% Binance spot maker/taker fee
        reference_drawdown_pct: Decimal = Decimal("0.03"),  # 3% adverse price drop
        slippage_buffer_pct: Decimal = Decimal("0.0005"),  # 0.05% slippage buffer
    ):
        self.initial_capital_usdt = initial_capital_usdt
        self.max_daily_entries = max_daily_entries
        self.daily_loss_halt_pct = daily_loss_halt_pct
        self.fee_rate = fee_rate
        self.reference_drawdown_pct = reference_drawdown_pct
        self.slippage_buffer_pct = slippage_buffer_pct

        # Runtime risk tracking state
        self.is_halted: bool = False
        self.halt_reason: Optional[str] = None
        self.daily_entries_count: int = 0
        self.current_utc_day: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _refresh_daily_quota(self, now_utc_dt: datetime) -> None:
        today_str = now_utc_dt.strftime("%Y-%m-%d")
        if today_str != self.current_utc_day:
            self.current_utc_day = today_str
            self.daily_entries_count = 0

    def evaluate_entry(
        self,
        signal_action: str,
        current_price: Decimal,
        market_rules: Dict[str, Any],
        available_usdt: Decimal,
        has_position: bool = False,
        today_realized_loss_usdt: Decimal = Decimal("0"),
        now_dt: Optional[datetime] = None,
    ) -> RiskCheckResult:
        now_utc = now_dt or datetime.now(timezone.utc)
        now_str = now_utc.isoformat()
        self._refresh_daily_quota(now_utc)

        checks: Dict[str, Any] = {
            "is_halted": self.is_halted,
            "has_position": has_position,
            "daily_entries_count": self.daily_entries_count,
            "max_daily_entries": self.max_daily_entries,
            "available_usdt": str(available_usdt),
            "current_price": str(current_price),
            "today_realized_loss_usdt": str(today_realized_loss_usdt),
        }

        # 1. Action check
        if signal_action != "BUY":
            return RiskCheckResult(
                approved=False,
                reason_code="SIGNAL_NOT_BUY",
                checks_detail=checks,
                evaluated_at_utc=now_str,
            )

        # 2. System halted check
        if self.is_halted:
            return RiskCheckResult(
                approved=False,
                reason_code=f"RISK_HALTED: {self.halt_reason}",
                checks_detail=checks,
                evaluated_at_utc=now_str,
            )

        # 3. Daily loss circuit breaker check
        max_allowed_daily_loss = self.initial_capital_usdt * self.daily_loss_halt_pct
        if today_realized_loss_usdt >= max_allowed_daily_loss:
            self.is_halted = True
            self.halt_reason = "DAILY_LOSS_LIMIT_REACHED"
            checks["max_allowed_daily_loss"] = str(max_allowed_daily_loss)
            return RiskCheckResult(
                approved=False,
                reason_code="REJECT_DAILY_LOSS_LIMIT_REACHED",
                checks_detail=checks,
                evaluated_at_utc=now_str,
            )

        # 4. Position count limit (max 1 open position)
        if has_position:
            return RiskCheckResult(
                approved=False,
                reason_code="REJECT_POSITION_ALREADY_OPEN",
                checks_detail=checks,
                evaluated_at_utc=now_str,
            )

        # 5. Daily entry quota limit
        if self.daily_entries_count >= self.max_daily_entries:
            return RiskCheckResult(
                approved=False,
                reason_code="REJECT_MAX_DAILY_ENTRIES_EXCEEDED",
                checks_detail=checks,
                evaluated_at_utc=now_str,
            )

        # 6. Budget cap & available balance validation
        usable_budget = min(self.initial_capital_usdt, available_usdt)
        min_notional = market_rules["min_notional"]
        step_size = market_rules["step_size"]

        if usable_budget < min_notional:
            return RiskCheckResult(
                approved=False,
                reason_code="REJECT_USDT_BELOW_MIN_NOTIONAL",
                checks_detail=checks,
                evaluated_at_utc=now_str,
            )

        # 7. Sizing & Precision Step-size Rounding Down
        # Allocate budget minus entry fee buffer
        net_budget = usable_budget / (Decimal("1") + self.fee_rate)
        raw_qty = net_budget / current_price

        # Round DOWN to nearest step_size multiple
        units = (raw_qty / step_size).to_integral_value(rounding=ROUND_DOWN)
        target_qty = units * step_size

        if target_qty < market_rules["min_qty"]:
            return RiskCheckResult(
                approved=False,
                reason_code="REJECT_QTY_BELOW_MIN_QTY",
                checks_detail=checks,
                evaluated_at_utc=now_str,
            )

        actual_buy_notional = target_qty * current_price
        if actual_buy_notional < min_notional:
            return RiskCheckResult(
                approved=False,
                reason_code="REJECT_ROUNDED_NOTIONAL_BELOW_MIN",
                checks_detail=checks,
                evaluated_at_utc=now_str,
            )

        # 8. Critical Exit Feasibility Pre-Check
        # Assume fee deducted from purchased BTC (qty * (1 - fee_rate))
        net_btc_received = target_qty * (Decimal("1") - self.fee_rate)

        # Simulate adverse price move: current_price * (1 - drawdown - slippage)
        adverse_factor = Decimal("1") - self.reference_drawdown_pct - self.slippage_buffer_pct
        adverse_exit_price = current_price * adverse_factor

        # Sizing exit: qty rounded down to step_size
        exit_units = (net_btc_received / step_size).to_integral_value(rounding=ROUND_DOWN)
        exit_qty = exit_units * step_size
        projected_exit_notional = exit_qty * adverse_exit_price

        checks["target_qty"] = str(target_qty)
        checks["actual_buy_notional"] = str(actual_buy_notional)
        checks["net_btc_received"] = str(net_btc_received)
        checks["adverse_exit_price"] = str(adverse_exit_price)
        checks["projected_exit_notional"] = str(projected_exit_notional)
        checks["min_notional"] = str(min_notional)

        if exit_qty < market_rules["min_qty"] or projected_exit_notional < min_notional:
            return RiskCheckResult(
                approved=False,
                reason_code="REJECT_EXIT_MIN_NOTIONAL",
                target_quantity=Decimal("0"),
                estimated_notional=Decimal("0"),
                checks_detail=checks,
                evaluated_at_utc=now_str,
            )

        # All checks passed
        return RiskCheckResult(
            approved=True,
            reason_code="APPROVED",
            target_quantity=target_qty,
            estimated_notional=actual_buy_notional,
            checks_detail=checks,
            evaluated_at_utc=now_str,
        )

    def record_entry_executed(self) -> None:
        self.daily_entries_count += 1
