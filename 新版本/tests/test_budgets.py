"""Paid-call budgets: the ledger is the meter, and a spent budget fails closed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from conftest import NOW

from jev_trading.domain.errors import BudgetExhaustedError
from jev_trading.domain.policy import PolicyCompiler
from jev_trading.services.budgets import BudgetService, model_cost_usd
from jev_trading.storage.repository import usage_with_cache_split


@pytest.fixture
async def repo(tmp_path):
    from jev_trading.storage.db import Database
    from jev_trading.storage.repository import Repository

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'budgets.db'}")
    await db.create_all()
    return Repository(db)


def _settings(server_limits, **overrides):
    # The settings object is constructed here rather than through the env so a
    # developer's .env cannot change what the budget tests measure.
    from jev_trading.config import Settings

    return Settings(**overrides)


async def _jev_call(repo, at: datetime, *, usage=None, error_code=None, response=None):
    await repo.insert_jev_evaluation(
        signal_id=None,
        model="typesev/test",
        request={"state": {}},
        response=(
            response if response is not None else {"usage": usage or {}}
        ),
        answers={},
        error_code=error_code,
    )
    # created_at is a column default; backdate explicitly for day/month edges.
    from sqlalchemy import update

    from jev_trading.storage.models import JevEvaluationRow

    async with repo.db.session() as session:
        await session.execute(
            update(JevEvaluationRow)
            .where(JevEvaluationRow.created_at > at - timedelta(seconds=1))
            .values(created_at=at)
        )


async def _agent_run(repo, at: datetime, usage: dict):
    await repo.insert_agent_run(
        cycle_id=None,
        policy_id=None,
        mode="testnet",
        provider="test",
        model="test",
        status="ok",
        usage=usage,
    )
    from sqlalchemy import update

    from jev_trading.storage.models import AgentRunRow

    async with repo.db.session() as session:
        await session.execute(
            update(AgentRunRow)
            .where(AgentRunRow.created_at > at - timedelta(seconds=1))
            .values(created_at=at)
        )


def test_usage_shapes_are_normalized():
    openai = usage_with_cache_split(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "total_tokens": 1200,
            "prompt_tokens_details": {"cached_tokens": 400},
        }
    )
    assert openai == {
        "prompt_tokens": 1000,
        "cached_tokens": 400,
        "completion_tokens": 200,
        "total_tokens": 1200,
    }
    deepseek = usage_with_cache_split(
        {
            "prompt_tokens": 30000,
            "completion_tokens": 2700,
            "prompt_cache_hit_tokens": 13000,
            "prompt_cache_miss_tokens": 17000,
        }
    )
    assert deepseek["cached_tokens"] == 13000
    anthropic = usage_with_cache_split(
        {"input_tokens": 50, "output_tokens": 7, "cache_read_input_tokens": 20}
    )
    assert anthropic["prompt_tokens"] == 50
    assert anthropic["cached_tokens"] == 20
    assert anthropic["total_tokens"] == 57
    assert usage_with_cache_split(None)["total_tokens"] == 0


def test_cost_prices_cached_tokens_separately(server_limits):
    settings = _settings(
        server_limits,
        model_input_price_per_mtok_usd=Decimal("1"),
        model_cached_input_price_per_mtok_usd=Decimal("0.25"),
        model_output_price_per_mtok_usd=Decimal("2"),
    )
    cost = model_cost_usd(
        settings,
        {
            "prompt_tokens": 1_000_000,
            "prompt_cache_hit_tokens": 400_000,
            "completion_tokens": 100_000,
        },
    )
    # 600k uncached at 1 + 400k cached at 0.25 + 100k output at 2
    assert cost == Decimal("0.9")


async def test_jev_daily_budget_raises_once_spent(repo, server_limits):
    budgets = BudgetService(repo, _settings(server_limits, jev_max_calls_per_utc_day=2))
    now = NOW
    await _jev_call(repo, now - timedelta(hours=3))
    spend = await budgets.check_jev(now)
    assert spend.limits["calls_today"] == 1

    await _jev_call(repo, now - timedelta(hours=2))
    with pytest.raises(BudgetExhaustedError) as raised:
        await budgets.check_jev(now)
    assert raised.value.details["max_calls_per_utc_day"] == 2

    # A new UTC day starts the count over; the month still counts both.
    tomorrow = now + timedelta(days=1)
    spend = await budgets.check_jev(tomorrow)
    assert spend.limits["calls_today"] == 0
    assert spend.limits["calls_month"] == 2


async def test_jev_counts_requests_that_reached_the_provider_only(repo, server_limits):
    budgets = BudgetService(repo, _settings(server_limits, jev_max_calls_per_utc_day=1))
    # A transport failure with no response never left the process: not billed.
    await _jev_call(
        repo, NOW - timedelta(hours=2), error_code="DEPENDENCY_UNAVAILABLE", response={}
    )
    spend = await budgets.check_jev(NOW)
    assert spend.limits["calls_today"] == 0

    # A provider-side error did reach the provider.
    await _jev_call(repo, NOW - timedelta(hours=1), error_code="PROVIDER_RATE_LIMITED")
    with pytest.raises(BudgetExhaustedError):
        await budgets.check_jev(NOW)


async def test_unpriced_provider_reports_cost_but_never_blocks(repo, server_limits):
    budgets = BudgetService(
        repo,
        _settings(
            server_limits,
            jev_max_calls_per_utc_day=0,
            jev_max_calls_per_utc_month=0,
            jev_max_monthly_cost_usd=Decimal("0.01"),
        ),
    )
    await _jev_call(
        repo, NOW - timedelta(hours=1), usage={"input_tokens": 9000, "output_tokens": 900}
    )
    spend = await budgets.check_jev(NOW)
    assert spend.priced is False
    assert spend.cost_usd == 0
    assert spend.limits["calls_month"] == 1


async def test_priced_provider_cost_cap_blocks(repo, server_limits):
    budgets = BudgetService(
        repo,
        _settings(
            server_limits,
            jev_max_calls_per_utc_day=0,
            jev_max_calls_per_utc_month=0,
            jev_input_price_per_mtok_usd=Decimal("1"),
            jev_max_monthly_cost_usd=Decimal("0.005"),
        ),
    )
    await _jev_call(
        repo, NOW - timedelta(hours=1), usage={"input_tokens": 6000, "output_tokens": 100}
    )
    with pytest.raises(BudgetExhaustedError) as raised:
        await budgets.check_jev(NOW)
    assert raised.value.details["provider"] == "jev"


async def test_model_token_budget_is_month_scoped(repo, server_limits):
    budgets = BudgetService(
        repo, _settings(server_limits, model_max_tokens_per_utc_month=1000)
    )
    await _agent_run(repo, NOW - timedelta(days=40), usage={"total_tokens": 5000})
    spend = await budgets.check_model(NOW)
    assert spend.tokens == 0, "last month's usage must not count against this month"

    await _agent_run(
        repo,
        NOW - timedelta(hours=1),
        usage={"prompt_tokens": 400, "completion_tokens": 700, "total_tokens": 1100},
    )
    with pytest.raises(BudgetExhaustedError):
        await budgets.check_model(NOW)


def test_month_start_is_utc_calendar_month():
    from jev_trading.services.budgets import month_start

    late_east = datetime(2026, 11, 1, 3, tzinfo=UTC).astimezone(
        __import__("datetime").timezone(timedelta(hours=8))
    )
    assert month_start(late_east) == datetime(2026, 11, 1, tzinfo=UTC)
