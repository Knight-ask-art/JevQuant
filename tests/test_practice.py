"""Beginner exercise: no live exchange, no credentials, no model calls."""
from decimal import Decimal

import pytest

from jevquant.practice import PaperPractice


def test_practice_starts_with_only_virtual_funds():
    session = PaperPractice()
    assert session.account.usdt_balance == Decimal("7.00")
    assert session.account.btc_balance == Decimal("0")
    assert session.equity == Decimal("7.00")
    assert session.pnl == Decimal("0")
    assert session.candle_number == 1
    assert session.events == []


def test_practice_buy_and_sell_with_fees_and_slippage():
    session = PaperPractice()
    bought = session.buy()
    assert bought.approved
    assert bought.reason == "PAPER_BUY_FILLED"
    assert bought.fee_usdt > 0
    assert session.account.has_position
    assert len(session.account.trades) == 1
    assert session.risk.daily_entries_count == 1

    advanced = session.next_candle()
    assert advanced.approved
    assert advanced.reason == "SYNTHETIC_CANDLE_ADVANCED"
    assert session.candle_number == 2
    assert session.equity == session.account.get_equity(session.price)

    sold = session.sell()
    assert sold.approved
    assert sold.reason == "PAPER_SELL_FILLED"
    assert session.account.btc_balance == Decimal("0")
    assert len(session.account.trades) == 2
    assert session.pnl == session.account.usdt_balance - Decimal("7.00")


def test_practice_prevents_selling_empty_position():
    session = PaperPractice()
    result = session.sell()
    assert not result.approved
    assert result.reason == "NO_PAPER_POSITION"
    assert session.account.trades == []


def test_practice_prevents_duplicate_buys_without_altering_balance():
    session = PaperPractice()
    assert session.buy().approved
    funds_before = session.account.usdt_balance
    denied = session.buy()
    assert not denied.approved
    assert denied.reason == "REJECT_POSITION_ALREADY_OPEN"
    assert session.account.usdt_balance == funds_before
    assert len(session.account.trades) == 1
    assert session.risk.daily_entries_count == 1


def test_practice_rejects_budget_below_minimum():
    session = PaperPractice(initial_usdt=Decimal("4"))
    result = session.buy()
    assert not result.approved
    assert result.reason == "REJECT_USDT_BELOW_MIN_NOTIONAL"
    assert session.account.trades == []
    assert session.risk.daily_entries_count == 0


def test_practice_fails_closed_when_demo_exit_below_notional():
    session = PaperPractice(prices=(Decimal("60000"), Decimal("40000")))
    assert session.buy().approved
    assert session.next_candle().approved
    sold = session.sell()
    assert not sold.approved
    assert sold.reason == "DEMO_EXIT_BELOW_MIN_NOTIONAL"
    assert session.account.has_position
    assert len(session.account.trades) == 1


def test_practice_daily_entry_quota_applies_even_after_selling():
    session = PaperPractice(prices=(Decimal("60000"),))
    for _ in range(3):
        assert session.buy().approved
        assert session.sell().approved
    denied = session.buy()
    assert not denied.approved
    assert denied.reason == "REJECT_MAX_DAILY_ENTRIES_EXCEEDED"
    assert session.risk.daily_entries_count == 3
    assert len(session.account.trades) == 6


def test_practice_halt_stops_new_buys_without_executing_trades():
    session = PaperPractice()
    session.risk.is_halted = True
    session.risk.halt_reason = "MANUAL_DEMO_PAUSE"
    denied = session.buy()
    assert not denied.approved
    assert denied.reason.startswith("RISK_HALTED:")
    assert session.account.trades == []


def test_practice_advance_stops_at_end_without_looping():
    session = PaperPractice(prices=(Decimal("60000"), Decimal("61000")))
    assert session.next_candle().approved
    assert session.completed
    denied = session.next_candle()
    assert not denied.approved
    assert denied.reason == "SCENARIO_COMPLETE"
    assert session.candle_number == 2


@pytest.mark.parametrize("initial,prices", [
    (Decimal("0"), (Decimal("60000"),)),
    (Decimal("-1"), (Decimal("60000"),)),
    (Decimal("NaN"), (Decimal("60000"),)),
    (Decimal("7"), ()),
    (Decimal("7"), (Decimal("0"),)),
    (Decimal("7"), (Decimal("Infinity"),)),
])
def test_practice_rejects_invalid_initial_state(initial, prices):
    with pytest.raises(ValueError):
        PaperPractice(initial_usdt=initial, prices=prices)
