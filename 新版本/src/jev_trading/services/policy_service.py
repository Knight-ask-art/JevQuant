"""Policy generation, compilation, activation and lookup."""

from __future__ import annotations

import inspect

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from ..adapters.models.base import PolicyContext, PolicyGenerator
from ..domain.errors import PolicyInvalidError
from ..domain.policy import (
    EffectivePolicy,
    PolicyCompiler,
    ServerLimits,
    StrategyPolicy,
)
from ..storage.models import StrategyPolicyRow
from ..storage.repository import Repository
from .audit import AuditService
from .events import EventBroker


class PolicyService:
    def __init__(
        self,
        repo: Repository,
        compiler: PolicyCompiler,
        server_limits: ServerLimits,
        audit: AuditService,
        broker: EventBroker,
    ) -> None:
        self.repo = repo
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
    ) -> PolicyContext:
        return PolicyContext(
            request_id=str(uuid4()),
            as_of=datetime.now(UTC),
            mode=mode,
            account_state=account_state,
            market_summary=market_summary,
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
    ) -> tuple[EffectivePolicy, StrategyPolicyRow]:
        context = self.build_context(
            mode=mode, account_state=account_state, market_summary=market_summary
        )
        try:
            accepts_purpose = False
            if purpose is not None:
                parameters = inspect.signature(
                    generator.generate_policy
                ).parameters.values()
                accepts_purpose = any(
                    parameter.name == "purpose"
                    or parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters
                )
            if accepts_purpose:
                result = await generator.generate_policy(  # type: ignore[call-arg]
                    context, purpose=purpose
                )
            else:
                result = await generator.generate_policy(context)
        except Exception as exc:
            await self.audit.record(
                "policy.generation_failed",
                "policy",
                None,
                {"error_code": getattr(getattr(exc, "code", None), "value", type(exc).__name__)},
            )
            raise
        try:
            raw = StrategyPolicy.model_validate(result.raw)
        except Exception as exc:
            raise PolicyInvalidError("Generated policy failed strict validation.") from exc
        effective = self.compiler.compile(
            raw, now=datetime.now(UTC), mode=_mode(context.mode)
        )
        row = await self.repo.insert_policy(
            effective, source=result.source, is_simulated=result.simulated
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
