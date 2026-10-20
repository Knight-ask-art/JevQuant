"""Paid-call budgets for the two providers this system pays for.

The spec's rate policy requires a configured spend limit before a paid model
is called, and a bound on Jev's call frequency. Both providers are metered
the same way: the ledger is the source of truth, so a restart never resets a
counter, and a check runs immediately before each paid call (plus once per
cycle for entries, so a blocked cycle is explained before a signal row
exists).

Two kinds of limit apply:

* **Calls / tokens** are always enforced once configured.
* **USD** is enforced only for a provider with a configured price. An unpriced
  provider reports 0; a cap that can never be reached would read as
  enforcement without being any.

A spent budget fails closed: Jev entries stop (the cycle says why), and the
strategy agent is not started (the existing policy, if any, keeps running).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from ..config import Settings
from ..domain.errors import BudgetExhaustedError
from ..storage.models import AgentRunRow, JevEvaluationRow
from ..storage.repository import Repository, usage_with_cache_split


def month_start(now: datetime) -> datetime:
    """First instant of the UTC calendar month ``now`` falls in."""

    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def day_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def model_usage(usage: Any) -> dict[str, int]:
    """The token split of one agent run, always shaped like the meter totals."""

    return usage_with_cache_split(usage)


def model_cost_usd(settings: Settings, usage: Any) -> Decimal:
    """What one agent run cost at the configured prices (0 when unpriced)."""

    split = usage_with_cache_split(usage)
    uncached = max(0, split["prompt_tokens"] - split["cached_tokens"])
    million = Decimal("1000000")
    cached_price = (
        settings.model_cached_input_price_per_mtok_usd
        or settings.model_input_price_per_mtok_usd
    )
    return (
        uncached * settings.model_input_price_per_mtok_usd
        + split["cached_tokens"] * cached_price
        + split["completion_tokens"] * settings.model_output_price_per_mtok_usd
    ) / million


def jev_cost_usd(settings: Settings, usage: Any) -> Decimal:
    split = usage_with_cache_split(usage)
    million = Decimal("1000000")
    return (
        split["prompt_tokens"] * settings.jev_input_price_per_mtok_usd
        + split["completion_tokens"] * settings.jev_output_price_per_mtok_usd
    ) / million


@dataclass(frozen=True)
class ProviderSpend:
    provider: str
    calls: int = 0
    tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    limits: dict[str, Any] = field(default_factory=dict)

    @property
    def priced(self) -> bool:
        return bool(self.limits.get("priced"))

    def to_payload(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "calls": self.calls,
            "tokens": self.tokens,
            "cost_usd": str(self.cost_usd.quantize(Decimal("0.000001"))),
            "priced": self.priced,
            "limits": dict(self.limits),
        }


class BudgetService:
    def __init__(self, repo: Repository, settings: Settings) -> None:
        self.repo = repo
        self.settings = settings

    async def _jev_rows(self, since: datetime) -> list[Any]:
        """One line per evaluation in the window: when, the error, the tokens.

        The request and response bodies are kilobytes each; only their usage
        is read here, so those two columns never leave the database.
        """

        async with self.repo.db.session() as session:
            return list(
                (
                    await session.execute(
                        select(
                            JevEvaluationRow.created_at,
                            JevEvaluationRow.error_code,
                            JevEvaluationRow.response_json["usage"].label("usage"),
                        ).where(JevEvaluationRow.created_at >= since)
                    )
                ).all()
            )

    async def _agent_rows(self, since: datetime) -> list[Any]:
        """One line per run in the window; the transcript stays in the row."""

        async with self.repo.db.session() as session:
            return list(
                (
                    await session.execute(
                        select(
                            AgentRunRow.created_at,
                            AgentRunRow.usage_json,
                        ).where(AgentRunRow.created_at >= since)
                    )
                ).all()
            )

    # -- Jev ---------------------------------------------------------------
    async def jev_spend(self, now: datetime | None = None) -> ProviderSpend:
        now = now or datetime.now(UTC)
        rows = await self._jev_rows(month_start(now))
        today = day_start(now)
        settings = self.settings
        calls_month = 0
        calls_today = 0
        cost = Decimal("0")
        for row in rows:
            # A request that never left the process (transport failure with no
            # response) is the one outcome that is certainly unbilled.
            if row.error_code == "DEPENDENCY_UNAVAILABLE" and not row.usage:
                continue
            calls_month += 1
            if row.created_at >= today:
                calls_today += 1
            cost += jev_cost_usd(self.settings, row.usage)
        return ProviderSpend(
            provider="jev",
            calls=calls_month,
            cost_usd=cost,
            limits={
                "calls_today": calls_today,
                "max_calls_per_utc_day": settings.jev_max_calls_per_utc_day,
                "calls_month": calls_month,
                "max_calls_per_utc_month": settings.jev_max_calls_per_utc_month,
                "max_monthly_cost_usd": str(settings.jev_max_monthly_cost_usd),
                "priced": bool(
                    settings.jev_input_price_per_mtok_usd
                    or settings.jev_output_price_per_mtok_usd
                ),
            },
        )

    async def check_jev(self, now: datetime | None = None) -> ProviderSpend:
        """Raise when Jev may not be called again; return the spend otherwise."""

        spend = await self.jev_spend(now)
        limits = spend.limits
        if limits["max_calls_per_utc_day"] and (
            limits["calls_today"] >= limits["max_calls_per_utc_day"]
        ):
            raise BudgetExhaustedError(
                "Jev daily call budget is spent.",
                details={
                    "provider": "jev",
                    "calls_today": limits["calls_today"],
                    "max_calls_per_utc_day": limits["max_calls_per_utc_day"],
                },
            )
        if limits["max_calls_per_utc_month"] and (
            limits["calls_month"] >= limits["max_calls_per_utc_month"]
        ):
            raise BudgetExhaustedError(
                "Jev monthly call budget is spent.",
                details={
                    "provider": "jev",
                    "calls_month": limits["calls_month"],
                    "max_calls_per_utc_month": limits["max_calls_per_utc_month"],
                },
            )
        cap = self.settings.jev_max_monthly_cost_usd
        if spend.priced and cap and spend.cost_usd >= cap:
            raise BudgetExhaustedError(
                "Jev monthly cost budget is spent.",
                details={
                    "provider": "jev",
                    "cost_usd": str(spend.cost_usd),
                    "max_monthly_cost_usd": str(cap),
                },
            )
        return spend

    # -- strategy agent ----------------------------------------------------
    async def model_spend(self, now: datetime | None = None) -> ProviderSpend:
        now = now or datetime.now(UTC)
        rows = await self._agent_rows(month_start(now))
        total = {
            "prompt_tokens": 0,
            "cached_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        cost = Decimal("0")
        for row in rows:
            split = usage_with_cache_split(row.usage_json)
            for key in total:
                total[key] += split[key]
            cost += model_cost_usd(self.settings, row.usage_json)
        settings = self.settings
        return ProviderSpend(
            provider="model",
            calls=len(rows),
            tokens=total["total_tokens"],
            cost_usd=cost,
            limits={
                **total,
                "max_tokens_per_utc_month": settings.model_max_tokens_per_utc_month,
                "max_monthly_cost_usd": str(settings.model_max_monthly_cost_usd),
                "priced": bool(
                    settings.model_input_price_per_mtok_usd
                    or settings.model_output_price_per_mtok_usd
                ),
            },
        )

    async def check_model(self, now: datetime | None = None) -> ProviderSpend:
        spend = await self.model_spend(now)
        limits = spend.limits
        if limits["max_tokens_per_utc_month"] and (
            spend.tokens >= limits["max_tokens_per_utc_month"]
        ):
            raise BudgetExhaustedError(
                "Strategy-agent monthly token budget is spent.",
                details={
                    "provider": "model",
                    "tokens": spend.tokens,
                    "max_tokens_per_utc_month": limits["max_tokens_per_utc_month"],
                },
            )
        cap = self.settings.model_max_monthly_cost_usd
        if spend.priced and cap and spend.cost_usd >= cap:
            raise BudgetExhaustedError(
                "Strategy-agent monthly cost budget is spent.",
                details={
                    "provider": "model",
                    "cost_usd": str(spend.cost_usd),
                    "max_monthly_cost_usd": str(cap),
                },
            )
        return spend

    async def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        """Both providers' spend for the status page and the audit trail."""

        return {
            "jev": (await self.jev_spend(now)).to_payload(),
            "model": (await self.model_spend(now)).to_payload(),
        }
