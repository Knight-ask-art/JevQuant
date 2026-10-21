"""Blocked buys are replayed so the gate can be judged on outcomes, not vibes."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import NOW, compiled_policy, market_snapshot, symbol_rules

from jev_trading.domain.enums import SignalState
from jev_trading.domain.market import Candle
from jev_trading.domain.policy import ExitDefaults
from jev_trading.services.counterfactual import (
    CounterfactualService,
    blocked_rule,
    simulate,
)
from jev_trading.storage.db import Database
from jev_trading.storage.repository import Repository

EXITS = ExitDefaults(
    stop_loss_bps=Decimal("120"),
    take_profit_bps=Decimal("240"),
    max_holding_seconds=3600,
)


def bar(offset: int, *, high: float = 100.5, low: float = 99.5, close: float = 100.0):
    start = NOW + timedelta(minutes=offset)
    return Candle(
        open_time=start,
        close_time=start + timedelta(minutes=1),
        open=float(close),
        high=high,
        low=low,
        close=close,
        volume=1.0,
    )


def replay(candles, price: str = "100", notional: str = "100"):
    return simulate(
        candles=candles,
        entry_price=Decimal(price),
        exit_rules=EXITS,
        horizon_end=NOW + timedelta(hours=1),
        taker_fee_bps=Decimal("10"),
        exit_slippage_bps=Decimal("8"),
        notional=Decimal(notional),
    )


async def test_stop_costs_slippage_and_fees():
    result = replay([bar(1, low=98.0), bar(2, high=100.5)])
    assert result is not None
    assert result.exit_reason == "stop_loss"
    # -120 bps stop, -8 bps exit slippage, -20 bps fees.
    assert Decimal("-152") < result.net_bps < Decimal("-145")
    assert result.pnl_usdt < 0


async def test_target_pays_after_costs():
    result = replay([bar(1, high=102.6)])
    assert result is not None
    assert result.exit_reason == "take_profit"
    assert Decimal("210") < result.net_bps < Decimal("220")


async def test_a_bar_touching_both_levels_is_a_stop():
    result = replay([bar(1, high=103.0, low=98.0)])
    assert result is not None
    assert result.exit_reason == "stop_loss"


def test_the_path_reports_how_far_price_ever_went_each_way():
    # Offsets are relative to NOW, whose price level differs from the 100
    # entry this test prices; only the shape of the path matters here.
    result = replay(
        [bar(1, high=100.0, low=99.0), bar(2, high=100.0, close=99.5)],
        price="99",
    )
    assert result is not None
    assert result.exit_reason == "timeout"
    assert round(float(result.mfe_bps)) == pytest.approx(101, abs=2)
    assert round(float(result.mae_bps)) == pytest.approx(0, abs=2)
    assert result.minutes_judged == 2


def test_timeout_exits_at_the_last_close():
    result = replay([bar(1, close=100.2), bar(2, close=100.4)])
    assert result is not None
    assert result.exit_reason == "timeout"
    assert result.pnl_usdt > 0


async def test_no_candles_means_no_verdict():
    assert replay([]) is None


def test_blocked_rule_names_the_leg_that_held(server_limits):
    policy = compiled_policy(server_limits)
    gate = policy.jev_gate
    base = {
        "entry_choice": "buy",
        "entry_probability": gate.min_choice_probability,
        "policy_fit": gate.min_policy_fit,
        "quality_score": gate.min_quality_score,
    }
    assert blocked_rule(policy, dict(base, entry_choice="reject")) == "entry_choice"
    assert (
        blocked_rule(policy, dict(base, entry_probability=gate.min_choice_probability - 0.01))
        == "min_choice_probability"
    )
    assert (
        blocked_rule(policy, dict(base, policy_fit=gate.min_policy_fit - 0.01)) == "min_policy_fit"
    )
    assert (
        blocked_rule(policy, dict(base, quality_score=gate.min_quality_score - 0.01))
        == "min_quality_score"
    )


class FakeMarket:
    def __init__(self, candles: list[Candle]) -> None:
        self.candles = candles

    async def filters(self, symbol: str, **_kwargs):
        return symbol_rules(symbol=symbol)

    async def klines(self, symbol: str, interval: str = "1m", limit: int = 120, warmup: int = 0):
        return self.candles


@pytest.fixture
async def repo(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'counterfactual.db'}")
    await db.create_all()
    try:
        yield Repository(db)
    finally:
        await db.dispose()


async def _blocked_signal(repo, policy, *, created_at=NOW, score=None):
    snapshot = market_snapshot(NOW)
    await repo.insert_market_snapshot(snapshot)
    signal = await repo.create_signal(
        symbol="BTCUSDT",
        policy_id=str(policy.policy_id),
        snapshot_id=str(snapshot.snapshot_id),
        side="buy",
        state=SignalState.REJECTED,
        created_at=created_at,
    )
    evaluation = await repo.insert_jev_evaluation(
        signal_id=signal.id, model="test", request={}, response={}, answers={}
    )
    await repo.attach_jev_evaluation(
        signal.id,
        evaluation.id,
        score=score
        or {
            "entry_choice": "buy",
            "entry_probability": 0.47,
            "policy_fit": 0.58,
            "quality_score": 2.05,
        },
    )
    return signal


async def test_a_blocked_buy_is_replayed_after_its_window(repo, server_limits, settings):
    policy = compiled_policy(server_limits, now=NOW)
    await repo.insert_policy(policy, source="test", is_simulated=True)
    signal = await _blocked_signal(repo, policy)
    market = FakeMarket([bar(1, low=67000.0), bar(2, high=69000.0)])
    service = CounterfactualService(settings=settings, repo=repo, market=market)

    written = await service.evaluate_pending(
        NOW + timedelta(hours=2), available_quote_usdt=Decimal("10000")
    )

    assert written == 1
    rows = await repo.list_counterfactuals()
    assert len(rows) == 1
    row = rows[0]
    assert row.signal_id == signal.id
    assert row.blocked_rule == "min_choice_probability"
    assert row.exit_reason == "stop_loss"
    assert row.net_bps < 0
    assert row.pnl_usdt is not None and row.pnl_usdt < 0
    assert row.assumed_notional_usdt == Decimal("500")

    # A second pass finds nothing new: one row per signal.
    assert await service.evaluate_pending(NOW + timedelta(hours=3)) == 0
    assert len(await repo.list_counterfactuals()) == 1


async def test_a_signal_still_inside_its_window_is_left_alone(repo, server_limits, settings):
    policy = compiled_policy(server_limits, now=NOW)
    await repo.insert_policy(policy, source="test", is_simulated=True)
    await _blocked_signal(repo, policy, created_at=NOW + timedelta(minutes=70))
    service = CounterfactualService(
        settings=settings,
        repo=repo,
        market=FakeMarket([bar(1, low=67000.0)]),
    )

    assert await service.evaluate_pending(NOW + timedelta(hours=2)) == 0
    assert await repo.list_counterfactuals() == []


async def test_a_rejected_choice_is_not_replayed(repo, server_limits, settings):
    policy = compiled_policy(server_limits, now=NOW)
    await repo.insert_policy(policy, source="test", is_simulated=True)
    await _blocked_signal(
        repo,
        policy,
        score={
            "entry_choice": "reject",
            "entry_probability": 0.95,
            "policy_fit": 0.21,
            "quality_score": 0.77,
        },
    )
    service = CounterfactualService(
        settings=settings,
        repo=repo,
        market=FakeMarket([bar(1, low=67000.0)]),
    )

    assert await service.evaluate_pending(NOW + timedelta(hours=2)) == 0
    assert await repo.list_counterfactuals() == []
