"""The policy authoring path honours the model budget, as the cycle does."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from jev_trading.config import Settings
from jev_trading.domain.errors import BudgetExhaustedError
from jev_trading.services.budgets import BudgetService
from jev_trading.services.policy_service import PolicyService
from jev_trading.storage.db import Database
from jev_trading.storage.repository import Repository


class _Generator:
    provider = "openai"
    model = "test-model"
    calls = 0

    async def generate_policy(self, context, **kwargs):
        type(self).calls += 1
        raise AssertionError("the model must not be called when the budget is spent")


async def test_a_spent_model_budget_stops_authoring_before_the_model_is_called(
    tmp_path,
):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'policy.db'}")
    await db.create_all()
    repo = Repository(db)
    settings = Settings(model_max_tokens_per_utc_month=1000)
    await repo.insert_agent_run(
        mode="testnet",
        provider="test",
        model="test",
        status="ok",
        usage={"total_tokens": 5000},
    )
    service = PolicyService(
        repo,
        compiler=None,
        server_limits=settings.server_limits,
        audit=SimpleNamespace(record=_noop),
        broker=SimpleNamespace(publish=_noop),
        settings=settings,
    )

    with pytest.raises(BudgetExhaustedError):
        await service.generate(
            generator=_Generator(),
            mode="paper",
            account_state={},
            market_summary={},
            budgets=BudgetService(repo, settings),
        )
    assert _Generator.calls == 0


async def _noop(*args, **kwargs):
    return None


def test_a_run_is_priced_at_the_configured_rates():
    settings = Settings(
        model_input_price_per_mtok_usd=Decimal("1"),
        model_output_price_per_mtok_usd=Decimal("2"),
    )
    service = PolicyService(
        repo=None,
        compiler=None,
        server_limits=settings.server_limits,
        audit=None,
        broker=None,
        settings=settings,
    )
    assert service._priced_cost(
        {"prompt_tokens": 1_000_000, "completion_tokens": 500_000}
    ) == Decimal("2")
    # Without settings there is nothing to price against.
    bare = PolicyService(None, None, settings.server_limits, None, None)
    assert bare._priced_cost({"prompt_tokens": 1_000_000}) == Decimal("0")
