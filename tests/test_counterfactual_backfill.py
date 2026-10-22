"""The backfill drains the backlog the hourly cycle cannot reach.

The cycle replays six signals an hour and loses its queue on restart. On a
quiet venue that is enough to keep every cohort report at "insufficient" for
days while decided BUYs sit unreplayed. The backfill runs the same replay with
a larger budget; it must not be a second, subtly different replay.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import NOW, compiled_policy
from test_counterfactual import FakeMarket, _blocked_signal, bar, repo  # noqa: F401

from jev_trading.services.counterfactual import (
    MAX_REPLAYS_PER_BACKFILL,
    MAX_REPLAYS_PER_CYCLE,
    CounterfactualService,
)
from jev_trading.storage.repository import Repository


def service(repo: Repository, settings) -> CounterfactualService:
    return CounterfactualService(
        settings=settings,
        repo=repo,
        market=FakeMarket([bar(1, low=67000.0), bar(2, high=69000.0)]),
    )


async def test_the_backfill_writes_more_than_one_cycle_would(repo, server_limits, settings):
    policy = compiled_policy(server_limits, now=NOW)
    await repo.insert_policy(policy, source="test", is_simulated=True)
    decided = MAX_REPLAYS_PER_CYCLE + 3
    for index in range(decided):
        await _blocked_signal(repo, policy, created_at=NOW + timedelta(minutes=index))

    written = await service(repo, settings).backfill(NOW + timedelta(hours=4))

    assert written == decided


async def test_the_cycle_still_stops_at_its_own_budget(repo, server_limits, settings):
    """The backfill widens the budget; it does not change the cycle's."""

    policy = compiled_policy(server_limits, now=NOW)
    await repo.insert_policy(policy, source="test", is_simulated=True)
    for index in range(MAX_REPLAYS_PER_CYCLE + 3):
        await _blocked_signal(repo, policy, created_at=NOW + timedelta(minutes=index))

    assert await service(repo, settings).evaluate_pending(NOW + timedelta(hours=4)) == (
        MAX_REPLAYS_PER_CYCLE
    )


async def test_the_backfill_is_idempotent(repo, server_limits, settings):
    policy = compiled_policy(server_limits, now=NOW)
    await repo.insert_policy(policy, source="test", is_simulated=True)
    await _blocked_signal(repo, policy)

    assert await service(repo, settings).backfill(NOW + timedelta(hours=4)) == 1
    assert await service(repo, settings).backfill(NOW + timedelta(hours=5)) == 0
    assert len(await repo.list_counterfactuals()) == 1


async def test_the_backfill_skips_signals_that_cannot_be_replayed_yet(
    repo, server_limits, settings
):
    """An unelapsed horizon is not a replay: it must not use up the budget."""

    policy = compiled_policy(server_limits, now=NOW)
    await repo.insert_policy(policy, source="test", is_simulated=True)
    await _blocked_signal(repo, policy)
    for index in range(MAX_REPLAYS_PER_CYCLE + 3):
        await _blocked_signal(repo, policy, created_at=NOW + timedelta(minutes=1 + index))

    # Neither set has elapsed at NOW; nothing is written and nothing is lost.
    assert await service(repo, settings).backfill(NOW) == 0
    assert await repo.list_counterfactuals() == []


async def test_the_backfill_swallows_a_single_failed_replay(repo, server_limits, settings):
    policy = compiled_policy(server_limits, now=NOW)
    await repo.insert_policy(policy, source="test", is_simulated=True)
    await _blocked_signal(repo, policy)
    await _blocked_signal(repo, policy, created_at=NOW + timedelta(minutes=1))

    class FlakyMarket(FakeMarket):
        calls = 0

        async def klines(self, *args, **kwargs):
            FlakyMarket.calls += 1
            if FlakyMarket.calls == 1:
                raise RuntimeError("venue hiccup")
            return await super().klines(*args, **kwargs)

    flaky = CounterfactualService(
        settings=settings, repo=repo, market=FlakyMarket([bar(1, low=67000.0)])
    )

    written = await flaky.backfill(NOW + timedelta(hours=4))

    # One attempt failed and was not retried here; the other was recorded.
    assert written == 1


def test_the_backfill_budget_is_bounded():
    assert 0 < MAX_REPLAYS_PER_CYCLE < MAX_REPLAYS_PER_BACKFILL
    assert MAX_REPLAYS_PER_BACKFILL <= 1000
