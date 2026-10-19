"""Policy generation, compilation, activation and lookup."""

from __future__ import annotations

import inspect

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from ..adapters.models.base import PolicyContext, PolicyGenerator
from ..config import Settings
from ..domain.errors import AppError, PolicyInvalidError
from ..domain.money import canonical_str
from ..domain.policy import (
    EffectivePolicy,
    PolicyCompiler,
    ServerLimits,
    StrategyPolicy,
)
from ..storage.models import StrategyPolicyRow
from ..storage.repository import Repository
from .audit import AuditService
from .budgets import model_cost_usd
from .events import EventBroker


# Which trace fields Agent Desk narrates. The full trace (tool names, budgets)
# stays out of the journal: it is large and not part of the economic record.
_TRACE_FLAGS = ("forced_answer", "repaired", "tool_fallback")


def _trace_flags(trace: Any) -> dict[str, bool]:
    if not isinstance(trace, dict):
        return {}
    return {name: True for name in _TRACE_FLAGS if trace.get(name)}


def _with_trace_flags(
    usage: dict[str, Any] | None,
    trace: dict[str, bool] | None,
    *,
    priced_cost_usd: Decimal | None = None,
) -> dict[str, Any]:
    """Provider usage as-is, with the run's flags hung off ``__trace__``.

    Kept off the provider's own keys so token accounting is never confused
    by journal-only fields. ``__cost_usd__`` is the run's cost at the
    configured prices, so Agent Desk can show what it spent.
    """

    merged = dict(usage or {})
    if trace:
        merged["__trace__"] = dict(trace)
    if priced_cost_usd is not None:
        merged["__cost_usd__"] = canonical_str(priced_cost_usd)
    return merged


class PolicyService:
    def __init__(
        self,
        repo: Repository,
        compiler: PolicyCompiler,
        server_limits: ServerLimits,
        audit: AuditService,
        broker: EventBroker,
        settings: Settings | None = None,
    ) -> None:
        self.repo = repo
        # Only used to price each run for the journal; None when a caller
        # builds the service without settings (tests, tooling).
        self.settings = settings
        self.compiler = compiler
        self.server_limits = server_limits
        self.audit = audit
        self.broker = broker

    def build_context(
        self,
        *,
        mode: str,
        account_state: dict[str, Any],
        market_summary: dict[str, Any],
        memory: dict[str, Any] | None = None,
    ) -> PolicyContext:
        return PolicyContext(
            request_id=str(uuid4()),
            as_of=datetime.now(UTC),
            mode=mode,
            account_state=account_state,
            market_summary=market_summary,
            memory=memory or {},
            absolute_server_limits={
                "max_position_notional_usdt": str(
                    self.server_limits.max_position_notional_usdt
                ),
                "max_gross_exposure_pct": str(self.server_limits.max_gross_exposure_pct),
                "max_daily_loss_pct": str(self.server_limits.max_daily_loss_pct),
                "max_positions": self.server_limits.max_positions,
                "max_drawdown_pct": str(self.server_limits.max_drawdown_pct),
                "allowed_symbols": sorted(self.server_limits.allowed_symbols),
                "max_policy_ttl_seconds": self.server_limits.max_policy_ttl_seconds,
            },
        )

    async def generate(
        self,
        *,
        generator: PolicyGenerator,
        mode: str,
        account_state: dict[str, Any],
        market_summary: dict[str, Any],
        purpose: str | None = None,
        toolbox: Any | None = None,
        cycle_id: str | None = None,
        budgets: Any | None = None,
    ) -> tuple[EffectivePolicy, StrategyPolicyRow]:
        # Checked here, not only by the callers: every path that authoring a
        # new policy passes through must honour the monthly budget. A spent
        # budget raises before the model is called, so nothing is billed.
        check = getattr(budgets, "check_model", None)
        if callable(check):
            await check()
        memory: dict[str, Any] = {}
        load_memory = getattr(toolbox, "memory", None)
        # The deterministic stand-in ignores memory; loading it would cost
        # four queries per simulated cycle for nothing.
        if callable(load_memory) and getattr(generator, "provider", "simulated") != "simulated":
            try:
                memory = await load_memory()
            except Exception:  # noqa: BLE001 - memory is optional context
                memory = {}
        context = self.build_context(
            mode=mode,
            account_state=account_state,
            market_summary=market_summary,
            memory=memory,
        )
        provider = str(getattr(generator, "provider", "simulated"))
        model = str(
            getattr(generator, "model", getattr(generator, "model_name", "unknown"))
        )
        try:
            parameters = list(
                inspect.signature(generator.generate_policy).parameters.values()
            )

            def accepts(name: str) -> bool:
                return any(
                    parameter.name == name
                    or parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters
                )

            kwargs: dict[str, Any] = {}
            if purpose is not None and accepts("purpose"):
                kwargs["purpose"] = purpose
            if toolbox is not None and accepts("toolbox"):
                kwargs["toolbox"] = toolbox
            result = await generator.generate_policy(context, **kwargs)  # type: ignore[call-arg]
        except Exception as exc:
            await self._record_generation_failure(
                exc,
                cycle_id=cycle_id,
                mode=mode,
                provider=provider,
                model=model,
            )
            raise
        trace = result.trace or {}
        try:
            raw = StrategyPolicy.model_validate(result.raw)
            effective = self.compiler.compile(
                raw, now=datetime.now(UTC), mode=_mode(context.mode)
            )
        except Exception as exc:
            # The model answered, but its answer is unusable. Journal the
            # attempt too, so a rejected policy shows up in the Agent Desk
            # instead of vanishing behind a failed request. The journal
            # carries the same code the caller is about to see.
            failure = (
                exc
                if isinstance(exc, AppError)
                else PolicyInvalidError("Generated policy failed strict validation.")
            )
            await self._record_generation_failure(
                failure,
                cycle_id=cycle_id,
                mode=mode,
                provider=provider,
                model=model,
                turns=result.turns,
                tool_calls=trace.get("tool_calls") if isinstance(trace, dict) else None,
                usage=result.usage,
                latency_ms=result.latency_ms,
                # The model answered, so this attempt was billed.
                priced_cost_usd=self._priced_cost(result.usage),
            )
            if failure is exc:
                raise
            raise failure from exc
        row = await self.repo.insert_policy(
            effective, source=result.source, is_simulated=result.simulated
        )
        await self._record_agent_run(
            cycle_id=cycle_id,
            policy_id=str(effective.policy_id),
            mode=mode,
            provider=provider,
            model=model,
            status="ok",
            turns=result.turns,
            tool_calls=trace.get("tool_calls") if isinstance(trace, dict) else None,
            usage=result.usage,
            latency_ms=result.latency_ms,
            rationale=raw.rationale,
            priced_cost_usd=self._priced_cost(result.usage),
            # Per-call turn numbers are already stored; these flags say how
            # the run ended so Agent Desk can narrate it.
            trace=_trace_flags(trace),
        )
        await self.audit.record(
            "policy.activated",
            "policy",
            str(effective.policy_id),
            {
                "policy_hash": effective.policy_hash,
                "source": result.source,
                "simulated": result.simulated,
                "model": result.model,
                "expires_at": effective.expires_at.isoformat(),
                "clamps": [clamp.model_dump(mode="json") for clamp in effective.clamps],
                "mode_scope": [m.value for m in effective.mode_scope],
            },
        )
        await self.broker.publish(
            "policy.updated",
            {
                "policy_id": str(effective.policy_id),
                "regime": effective.regime.label.value,
                "expires_at": effective.expires_at.isoformat(),
                "simulated": result.simulated,
            },
        )
        return effective, row

    async def _record_generation_failure(
        self,
        exc: Exception,
        *,
        cycle_id: str | None,
        mode: str,
        provider: str,
        model: str,
        turns: int = 0,
        tool_calls: Any = None,
        usage: dict[str, Any] | None = None,
        latency_ms: int = 0,
        priced_cost_usd: Decimal | None = None,
    ) -> None:
        """Record a failed authoring attempt in the audit chain and journal."""

        error_code = getattr(getattr(exc, "code", None), "value", type(exc).__name__)
        await self.audit.record(
            "policy.generation_failed",
            "policy",
            None,
            {"error_code": error_code},
        )
        await self._record_agent_run(
            cycle_id=cycle_id,
            policy_id=None,
            mode=mode,
            provider=provider,
            model=model,
            status="error",
            turns=turns,
            tool_calls=tool_calls,
            usage=usage,
            latency_ms=latency_ms,
            error_code=str(error_code),
            priced_cost_usd=priced_cost_usd,
        )

    async def _record_agent_run(
        self,
        *,
        cycle_id: str | None,
        policy_id: str | None,
        mode: str,
        provider: str,
        model: str,
        status: str,
        turns: int = 0,
        tool_calls: Any = None,
        usage: dict[str, Any] | None = None,
        latency_ms: int = 0,
        rationale: str | None = None,
        error_code: str | None = None,
        trace: dict[str, bool] | None = None,
        priced_cost_usd: Decimal | None = None,
    ) -> None:
        """Persist the agent journal entry; never fail a cycle for it."""

        try:
            await self.repo.insert_agent_run(
                cycle_id=cycle_id,
                policy_id=policy_id,
                mode=mode,
                provider=provider,
                model=model,
                status=status,
                turns=turns,
                tool_calls=tool_calls if isinstance(tool_calls, list) else [],
                usage=_with_trace_flags(usage, trace, priced_cost_usd=priced_cost_usd),
                latency_ms=latency_ms,
                rationale=rationale,
                error_code=error_code,
            )
        except Exception:  # noqa: BLE001 - observability must not break trading
            pass

    def _priced_cost(self, usage: dict[str, Any] | None) -> Decimal:
        """Cost of one run at the configured prices; 0 when unpriced."""

        if self.settings is None:
            return Decimal("0")
        try:
            return model_cost_usd(self.settings, usage)
        except Exception:  # noqa: BLE001 - journal-only figure
            return Decimal("0")

    async def get_active(self, mode: str) -> EffectivePolicy | None:
        row = await self.repo.get_active_policy()
        if row is None:
            return None
        if row.expires_at <= datetime.now(UTC):
            await self.repo.expire_policies()
            return None
        effective = EffectivePolicy.model_validate(row.effective_json)
        if not effective.applies_to(_mode(mode)):
            return None
        return effective

    async def get_active_row(self) -> StrategyPolicyRow | None:
        return await self.repo.get_active_policy()

    @staticmethod
    def describe(effective: EffectivePolicy) -> dict[str, Any]:
        return effective.model_dump(mode="json")


def _mode(value: str):
    from ..domain.enums import Mode

    return Mode(value)
