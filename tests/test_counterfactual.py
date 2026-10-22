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

    async def klines(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 120,
        warmup: int = 0,
        start=None,
    ):
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


async def test_reject_choices_do_not_take_the_replay_slots(
    repo, server_limits, settings
):
    """The oldest-first queue must not be filled by rows that replay nothing.

    A reject signal returns None without writing a row, so it stays at the
    head of the queue forever. Enough of them and the real buys behind it are
    never reached, which is exactly what happened in production.
    """

    policy = compiled_policy(server_limits, now=NOW)
    await repo.insert_policy(policy, source="test", is_simulated=True)
    for offset in range(8):
        await _blocked_signal(
            repo,
            policy,
            created_at=NOW - timedelta(hours=8) + timedelta(minutes=offset),
            score={
                "entry_choice": "reject",
                "entry_probability": 0.63,
                "policy_fit": 0.28,
                "quality_score": 1.26,
            },
        )
    await _blocked_signal(repo, policy)

    candidates = await repo.list_rejected_signals_without_counterfactual(
        since=NOW - timedelta(hours=14), until=NOW + timedelta(hours=2), limit=6
    )

    assert [signal.score_json["entry_choice"] for signal in candidates] == ["buy"]
    service = CounterfactualService(
        settings=settings,
        repo=repo,
        market=FakeMarket([bar(1, low=98.0)]),
    )
    assert await service.evaluate_pending(NOW + timedelta(hours=2)) == 1


async def test_a_signal_with_a_long_horizon_is_still_replayed_later(
    repo, server_limits, settings
):
    """The lookback must cover the longest holding window a policy may carry.

    It used to be a flat 14 h, justified by the venue's 1000-bar page limit --
    but the fetcher pages around that limit, so the bound was stale. Any policy
    with a holding window longer than the lookback produced signals that could
    never be replayed at all: they aged out before their horizon elapsed.
    """

    exits = ExitDefaults(
        stop_loss_bps=Decimal("120"),
        take_profit_bps=Decimal("240"),
        max_holding_seconds=20 * 3600,
    )
    policy = compiled_policy(server_limits, now=NOW).model_copy(
        update={"exit_defaults": exits}
    )
    await repo.insert_policy(policy, source="test", is_simulated=True)
    await _blocked_signal(repo, policy)
    service = CounterfactualService(
        settings=settings,
        repo=repo,
        market=FakeMarket([bar(1, low=98.0)]),
    )

    # Still inside its window: not replayed yet, and not written off either.
    assert await service.evaluate_pending(NOW + timedelta(hours=10)) == 0
    assert await repo.list_counterfactuals() == []

    # Past the horizon, and well past the old 14 h bound.
    assert await service.evaluate_pending(NOW + timedelta(hours=21)) == 1


async def test_a_signal_that_can_never_be_replayed_does_not_block_the_queue(
    repo, server_limits, settings
):
    """A row with no policy would otherwise return None forever, at the head.

    It writes nothing, so it is re-selected every cycle; enough of them and the
    per-cycle budget never reaches a signal that could produce an outcome.
    """

    policy = compiled_policy(server_limits, now=NOW)
    await repo.insert_policy(policy, source="test", is_simulated=True)
    for offset in range(8):
        await _blocked_signal(
            repo, policy, created_at=NOW - timedelta(hours=8) + timedelta(minutes=offset)
        )
    # Point every stored signal at a policy that does not exist, so each one
    # returns None without writing a row.
    from sqlalchemy import update

    from jev_trading.storage.models import SignalRow

    async with repo.db.session() as session:
        await session.execute(update(SignalRow).values(policy_id="missing-policy"))
        await session.commit()

    await _blocked_signal(repo, policy)
    service = CounterfactualService(
        settings=settings,
        repo=repo,
        market=FakeMarket([bar(1, low=98.0)]),
    )

    assert await service.evaluate_pending(NOW + timedelta(hours=2)) == 1
    rows = await repo.list_counterfactuals()
    assert len(rows) == 1


async def test_a_window_with_no_bars_is_recorded_as_no_data(repo, server_limits, settings):
    policy = compiled_policy(server_limits, now=NOW)
    await repo.insert_policy(policy, source="test", is_simulated=True)
    await _blocked_signal(repo, policy)
    service = CounterfactualService(settings=settings, repo=repo, market=FakeMarket([]))

    written = await service.evaluate_pending(NOW + timedelta(hours=2))

    # Stored, not skipped: otherwise the same signal is scanned every cycle.
    assert written == 1
    rows = await repo.list_counterfactuals()
    assert len(rows) == 1
    assert rows[0].exit_reason == "no_data"
    assert rows[0].net_bps is None
    assert rows[0].gap_seconds == 3600
    assert await service.evaluate_pending(NOW + timedelta(hours=3)) == 0


async def test_a_regime_rejection_is_not_blamed_on_the_jev_gate(
    repo, server_limits, settings
):
    """The two gates are judged on different cohorts, so the record must say which.

    The Jev thresholds can be recomputed from the score, but a regime refusal
    depends on the snapshot's ATR, which the score does not carry. Reading it
    back from the recorded event is what keeps a cost refusal from looking like
    a threshold refusal -- the cohorts would otherwise be the same to the
    counterfactual report, which is the only thing that can answer "which gate
    is costing us money".
    """

    policy = compiled_policy(server_limits, now=NOW)
    await repo.insert_policy(policy, source="test", is_simulated=True)
    signal = await _blocked_signal(repo, policy)
    await repo.transition_signal(
        signal.id,
        SignalState.REJECTED,
        {
            "regime_blocked": True,
            "regime_reason": "expected_move_below_cost",
            "regime_expected_abs_move_bps": 12.0,
            "regime_required_move_bps": 45.0,
        },
    )
    service = CounterfactualService(
        settings=settings,
        repo=repo,
        market=FakeMarket([bar(1, low=67000.0), bar(2, high=69000.0)]),
    )

    assert await service.evaluate_pending(NOW + timedelta(hours=2)) == 1
    rows = await repo.list_counterfactuals()
    # The stored score fails min_choice_probability; the regime event wins.
    assert rows[0].blocked_rule == "regime_expected_move_below_cost"



def test_gap_accounting_excludes_entry_minute_and_post_stop_horizon():
    """A complete first eligible minute is sufficient for an immediate stop."""

    from jev_trading.services.counterfactual import missing_minute_seconds

    first = bar(1)
    assert missing_minute_seconds(
        [first], decided_at=NOW, observed_until=first.open_time + timedelta(minutes=1)
    ) == 0
    # The same bar does not cover the rest of a timeout's 60-minute horizon.
    assert missing_minute_seconds(
        [first], decided_at=NOW, observed_until=NOW + timedelta(hours=1)
    ) == 58 * 60


def test_gap_accounting_detects_internal_and_leading_gaps_even_with_duplicates():
    from jev_trading.services.counterfactual import missing_minute_seconds

    first, third = bar(1), bar(3)
    assert missing_minute_seconds(
        [first, first, third],
        decided_at=NOW,
        observed_until=third.open_time + timedelta(minutes=1),
    ) == 60
    assert missing_minute_seconds(
        [third], decided_at=NOW, observed_until=third.open_time + timedelta(minutes=1)
    ) == 120


def test_gap_accounting_accepts_binance_close_time_one_millisecond_early():
    from jev_trading.services.counterfactual import missing_minute_seconds

    first = bar(1)
    binance_bar = Candle(
        open_time=first.open_time,
        close_time=first.close_time - timedelta(milliseconds=1),
        open=first.open,
        high=first.high,
        low=first.low,
        close=first.close,
        volume=first.volume,
    )
    assert missing_minute_seconds(
        [binance_bar],
        decided_at=NOW,
        observed_until=first.open_time + timedelta(minutes=1),
    ) == 0


async def test_early_stop_has_zero_gap_and_remains_an_eligible_outcome(
    repo, server_limits, settings
):
    policy = compiled_policy(server_limits, now=NOW)
    await repo.insert_policy(policy, source="test", is_simulated=True)
    await _blocked_signal(repo, policy)
    service = CounterfactualService(
        settings=settings, repo=repo, market=FakeMarket([bar(1, low=67000.0)])
    )

    assert await service.evaluate_pending(NOW + timedelta(hours=2)) == 1
    outcome = (await repo.list_counterfactuals())[0]
    assert outcome.exit_reason == "stop_loss"
    assert outcome.gap_seconds == 0
    assert outcome.net_bps is not None


async def test_missing_minutes_before_a_stop_are_reported(
    repo, server_limits, settings
):
    policy = compiled_policy(server_limits, now=NOW)
    await repo.insert_policy(policy, source="test", is_simulated=True)
    await _blocked_signal(repo, policy)
    service = CounterfactualService(
        settings=settings, repo=repo, market=FakeMarket([bar(3, low=67000.0)])
    )

    assert await service.evaluate_pending(NOW + timedelta(hours=2)) == 1
    outcome = (await repo.list_counterfactuals())[0]
    assert outcome.exit_reason == "stop_loss"
    assert outcome.gap_seconds == 120


async def test_timeout_keeps_missing_tail_as_a_gap(repo, server_limits, settings):
    policy = compiled_policy(server_limits, now=NOW)
    await repo.insert_policy(policy, source="test", is_simulated=True)
    await _blocked_signal(repo, policy)
    # Flat relative to the entry, so neither stop nor target is reached.
    service = CounterfactualService(
        settings=settings,
        repo=repo,
        market=FakeMarket([bar(1, high=68050.0, low=67980.0, close=68020.0)]),
    )

    assert await service.evaluate_pending(NOW + timedelta(hours=2)) == 1
    outcome = (await repo.list_counterfactuals())[0]
    assert outcome.exit_reason == "timeout"
    assert outcome.gap_seconds == 58 * 60



def test_replay_sorts_out_of_order_candles_before_charging_a_stop():
    # Target was reached before a later stop. Original input order must not
    # reverse the outcome or produce an erroneous MFE/MAE sequence.
    result = replay([bar(3, low=98.0), bar(1, high=102.6)])
    assert result is not None
    assert result.exit_reason == "take_profit"


def test_replay_deduplicates_identical_minute_bars():
    first = bar(1, high=102.6)
    result = replay([first, first, bar(3, low=98.0)])
    assert result is not None
    assert result.exit_reason == "take_profit"
    assert result.minutes_judged == 1


def test_conflicting_duplicate_minute_bars_fail_closed():
    import pytest

    with pytest.raises(ValueError, match="conflicting duplicate"):
        replay([bar(1, high=102.6), bar(1, high=100.5)])


def test_minute_normalization_rejects_unaligned_open_and_allows_binance_close_time():
    import pytest
    from jev_trading.services.klines import normalize_minute_candles

    candle = bar(1)
    binance = Candle(
        open_time=candle.open_time,
        close_time=candle.close_time - timedelta(milliseconds=1),
        open=candle.open,
        high=candle.high,
        low=candle.low,
        close=candle.close,
        volume=candle.volume,
    )
    assert normalize_minute_candles([binance]) == [binance]
    unaligned = Candle(
        open_time=candle.open_time + timedelta(seconds=30),
        close_time=candle.close_time,
        open=candle.open,
        high=candle.high,
        low=candle.low,
        close=candle.close,
        volume=candle.volume,
    )
    with pytest.raises(ValueError, match="minute-aligned"):
        normalize_minute_candles([unaligned])


async def test_counterfactual_uses_chronological_candles_even_if_provider_reorders(
    repo, server_limits, settings
):
    policy = compiled_policy(server_limits, now=NOW)
    await repo.insert_policy(policy, source="test", is_simulated=True)
    await _blocked_signal(repo, policy)
    service = CounterfactualService(
        settings=settings,
        repo=repo,
        market=FakeMarket([
            bar(3, low=67000.0),
            bar(1, high=71000.0, low=68000.0, close=68010.0),
        ]),
    )
    assert await service.evaluate_pending(NOW + timedelta(hours=2)) == 1
    outcome = (await repo.list_counterfactuals())[0]
    assert outcome.exit_reason == "take_profit"
    assert outcome.gap_seconds == 0
