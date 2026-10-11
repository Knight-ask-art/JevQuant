from datetime import datetime, timezone
from decimal import Decimal
import pytest
from jevquant.orders import OrderStateMachine, PaperAccount
from jevquant.risk import RiskManager


def get_standard_market_rules(min_notional="5.00000000", step_size="0.00001", min_qty="0.00001"):
    return {
        "symbol": "BTC/USDT",
        "status": "TRADING",
        "min_notional": Decimal(min_notional),
        "step_size": Decimal(step_size),
        "min_qty": Decimal(min_qty),
        "min_price": Decimal("0.01"),
        "max_price": Decimal("1000000.00"),
        "tick_size": Decimal("0.01"),
    }


def test_risk_manager_exit_feasibility_approval():
    # Capital: 7.00 USDT, Price: 60,000 USDT.
    # Buy notional ~ 6.99 USDT > 5.00 min_notional.
    # Exit after 3% drawdown: 6.99 * 0.9695 ~ 6.77 USDT > 5.00 min_notional.
    risk = RiskManager(initial_capital_usdt=Decimal("7.00"))
    rules = get_standard_market_rules(min_notional="5.00")

    result = risk.evaluate_entry(
        signal_action="BUY",
        current_price=Decimal("60000.00"),
        market_rules=rules,
        available_usdt=Decimal("7.00"),
        has_position=False,
    )
    assert result.approved is True
    assert result.reason_code == "APPROVED"
    assert result.target_quantity > Decimal("0")


def test_risk_manager_reject_exit_feasibility_when_borderline():
    # At price 50000.00 and usable budget 5.20 USDT:
    # Target buy notional is 0.00010 * 50000 = 5.00 USDT (meets min_notional 5.00 USDT).
    # But after 3% drawdown and slippage/fees:
    # Projected exit notional is ~ 4.36 USDT < 5.00 min_notional!
    # MUST be rejected with REJECT_EXIT_MIN_NOTIONAL!
    risk = RiskManager(initial_capital_usdt=Decimal("5.20"))
    rules = get_standard_market_rules(min_notional="5.00")

    result = risk.evaluate_entry(
        signal_action="BUY",
        current_price=Decimal("50000.00"),
        market_rules=rules,
        available_usdt=Decimal("5.20"),
        has_position=False,
    )
    assert result.approved is False
    assert result.reason_code == "REJECT_EXIT_MIN_NOTIONAL"


def test_risk_manager_rejects_repeated_entry_when_holding():
    risk = RiskManager(initial_capital_usdt=Decimal("7.00"))
    rules = get_standard_market_rules()

    result = risk.evaluate_entry(
        signal_action="BUY",
        current_price=Decimal("60000.00"),
        market_rules=rules,
        available_usdt=Decimal("7.00"),
        has_position=True,
    )
    assert result.approved is False
    assert result.reason_code == "REJECT_POSITION_ALREADY_OPEN"


def test_risk_manager_daily_loss_circuit_breaker():
    # 3% of 7.00 USDT = 0.21 USDT loss threshold
    risk = RiskManager(initial_capital_usdt=Decimal("7.00"), daily_loss_halt_pct=Decimal("0.03"))
    rules = get_standard_market_rules()

    result = risk.evaluate_entry(
        signal_action="BUY",
        current_price=Decimal("60000.00"),
        market_rules=rules,
        available_usdt=Decimal("7.00"),
        has_position=False,
        today_realized_loss_usdt=Decimal("0.25"),  # Exceeds 0.21
    )
    assert result.approved is False
    assert result.reason_code == "REJECT_DAILY_LOSS_LIMIT_REACHED"
    assert risk.is_halted is True


def test_paper_account_execution_and_fee_deduction():
    paper = PaperAccount(initial_usdt=Decimal("10.00"), maker_taker_fee=Decimal("0.001"), slippage_pct=Decimal("0.0005"))
    market_price = Decimal("60000.00")
    buy_qty = Decimal("0.00010")  # Cost ~ 6.00 USDT

    fill_buy = paper.execute_buy(
        symbol="BTC/USDT",
        target_qty=buy_qty,
        market_price=market_price,
        client_order_id="ord_test_001",
    )
    assert fill_buy.side == "BUY"
    assert paper.has_position is True
    assert paper.btc_balance == buy_qty
    # USDT should be reduced by cost + fee
    assert paper.usdt_balance < Decimal("4.00")

    # Sell full position at 61000
    fill_sell = paper.execute_sell(
        symbol="BTC/USDT",
        sell_qty=buy_qty,
        market_price=Decimal("61000.00"),
        client_order_id="ord_test_002",
    )
    assert fill_sell.side == "SELL"
    assert paper.has_position is False
    assert paper.btc_balance == Decimal("0")
    assert len(paper.trades) == 2


def test_order_state_machine_transitions():
    sm = OrderStateMachine(initial_state="FLAT")
    assert sm.state == "FLAT"

    sm.transition_to("ENTRY_SUBMITTING")
    assert sm.state == "ENTRY_SUBMITTING"

    sm.transition_to("LONG")
    assert sm.state == "LONG"

    sm.transition_to("EXIT_SUBMITTING")
    assert sm.state == "EXIT_SUBMITTING"

    sm.transition_to("FLAT")
    assert sm.state == "FLAT"

    with pytest.raises(ValueError, match="Invalid state"):
        sm.transition_to("UNKNOWN_STATE")
