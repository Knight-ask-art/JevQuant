"""Strict StrategyPolicy contract and the server-side policy compiler.

The LLM proposes a policy. The server compiles it against hard limits.
The compiler may only *narrow* the proposal; it can never widen it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .enums import JevChoice, Mode, PolicyStatus, RegimeLabel, Side
from .errors import PolicyExpiredError, PolicyInvalidError

SymbolStr = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{5,20}$")]
SCHEMA_VERSION = "strategy-policy/1.0"


def utcnow() -> datetime:
    return datetime.now(UTC)


def canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_json(payload: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class Regime(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: RegimeLabel
    confidence: float = Field(ge=0.0, le=1.0)
    notes: str = Field(default="", max_length=1000)


class SymbolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: SymbolStr
    allowed_sides: list[Side] = Field(min_length=1)
    max_position_notional_usdt: Decimal = Field(gt=0)
    max_spread_bps: Decimal = Field(ge=0)
    max_slippage_bps: Decimal = Field(ge=0)
    entry_cooldown_seconds: int = Field(ge=0, le=86_400)


class PortfolioPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_gross_exposure_pct: Decimal = Field(ge=0, le=100)
    max_positions: int = Field(ge=0, le=100)
    max_daily_loss_pct: Decimal = Field(ge=0, le=100)
    max_drawdown_pct: Decimal = Field(ge=0, le=100)


class JevGatePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entry_choice: JevChoice
    min_choice_probability: float = Field(ge=0.0, le=1.0)
    min_policy_fit: float = Field(ge=0.0, le=1.0)
    min_quality_score: float = Field(ge=0.0, le=5.0)


class ExitDefaults(BaseModel):
    """The exit geometry a policy proposes, before the server clamps it.

    The two ratchet fields are optional and default to off. A policy that does
    not mention them gets exactly the v1 behaviour -- a fixed stop and target --
    so enabling a trail is always a deliberate act by whoever wrote the policy,
    never a side effect of upgrading the code.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    stop_loss_bps: Decimal = Field(gt=0, le=10_000)
    take_profit_bps: Decimal = Field(gt=0, le=10_000)
    max_holding_seconds: int = Field(gt=0, le=604_800)
    #: Move the stop to entry + round-trip cost once the peak clears this.
    breakeven_trigger_bps: Decimal | None = Field(default=None, gt=0, le=10_000)
    #: Start trailing once the peak clears this.
    trailing_trigger_bps: Decimal | None = Field(default=None, gt=0, le=10_000)
    #: Keep the trailing stop this far below the peak.
    trailing_distance_bps: Decimal | None = Field(default=None, gt=0, le=10_000)

    @model_validator(mode="after")
    def _trail_is_coherent(self) -> ExitDefaults:
        if self.trailing_trigger_bps is None and self.trailing_distance_bps is None:
            return self
        if self.trailing_trigger_bps is None or self.trailing_distance_bps is None:
            raise ValueError(
                "trailing_trigger_bps and trailing_distance_bps must be set together"
            )
        if self.trailing_distance_bps >= self.trailing_trigger_bps:
            # Otherwise the trail arms at or below entry and can exit a trade
            # that never made money, which is a stop with extra steps.
            raise ValueError(
                "trailing_distance_bps must be smaller than trailing_trigger_bps"
            )
        return self

    def geometry(self):
        """The runtime form of these rules (see ``domain/exits.py``)."""

        from .exits import ExitGeometry

        return ExitGeometry(
            stop_loss_bps=self.stop_loss_bps,
            take_profit_bps=self.take_profit_bps,
            max_holding_seconds=self.max_holding_seconds,
            breakeven_trigger_bps=self.breakeven_trigger_bps,
            trailing_trigger_bps=self.trailing_trigger_bps,
            trailing_distance_bps=self.trailing_distance_bps,
        )


class StrategyPolicy(BaseModel):
    """The exact JSON shape the LLM must return (strategy-policy/1.0)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    policy_id: UUID
    generated_at: AwareDatetime
    expires_at: AwareDatetime
    mode_scope: list[Mode] = Field(min_length=1)
    regime: Regime
    universe: list[SymbolPolicy] = Field(min_length=1)
    portfolio: PortfolioPolicy
    jev_gate: JevGatePolicy
    exit_defaults: ExitDefaults
    invalidation: list[str] = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=4000)

    @field_validator("schema_version")
    @classmethod
    def _check_version(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SCHEMA_VERSION!r}")
        return value

    @field_validator("mode_scope")
    @classmethod
    def _dedupe_modes(cls, value: list[Mode]) -> list[Mode]:
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def _time_order(self) -> StrategyPolicy:
        if self.generated_at >= self.expires_at:
            raise ValueError("expires_at must be after generated_at")
        return self

    def policy_hash(self) -> str:
        return sha256_json(self.model_dump(mode="json"))


class ServerLimits(BaseModel):
    """Hard envelope. These values are never taken from model output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_position_notional_usdt: Decimal = Field(gt=0)
    max_gross_exposure_pct: Decimal = Field(ge=0, le=100)
    max_daily_loss_pct: Decimal = Field(ge=0, le=100)
    max_positions: int = Field(ge=0, le=100)
    max_drawdown_pct: Decimal = Field(ge=0, le=100)
    max_policy_ttl_seconds: int = Field(gt=0, le=86_400)
    allowed_symbols: frozenset[str] = Field(min_length=1)
    allowed_entry_sides: frozenset[Side] = Field(default_factory=lambda: frozenset({Side.BUY}))
    max_spread_bps: Decimal = Field(default=Decimal("5"), ge=0)
    max_slippage_bps: Decimal = Field(default=Decimal("15"), ge=0)
    min_stop_loss_bps: Decimal = Field(default=Decimal("20"), gt=0)
    max_stop_loss_bps: Decimal = Field(default=Decimal("300"), gt=0)
    max_take_profit_bps: Decimal = Field(default=Decimal("1000"), gt=0)
    max_holding_seconds: int = Field(default=86_400, gt=0)
    #: The longest trail a policy may ask for. A trail wider than the target
    #: can only ever fire below entry, so the server refuses it rather than
    #: letting a policy describe an exit that cannot make money.
    max_trailing_distance_bps: Decimal = Field(default=Decimal("400"), gt=0)
    min_entry_cooldown_seconds: int = Field(default=0, ge=0)
    min_choice_probability: float = Field(default=0.5, ge=0.0, le=1.0)
    min_policy_fit: float = Field(default=0.5, ge=0.0, le=1.0)
    min_quality_score: float = Field(default=0.0, ge=0.0, le=5.0)
    max_intent_ttl_seconds: int = Field(default=30, gt=0, le=300)

    @field_validator("allowed_symbols")
    @classmethod
    def _normalize_symbols(cls, value: frozenset[str]) -> frozenset[str]:
        normalized = frozenset(s.strip().upper() for s in value if s.strip())
        if not normalized:
            raise ValueError("allowed_symbols cannot be empty")
        return normalized


class Clamp(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    requested: str
    effective: str
    reason: str


class EffectiveSymbolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: SymbolStr
    allowed_sides: list[Side]
    max_position_notional_usdt: Decimal
    max_spread_bps: Decimal
    max_slippage_bps: Decimal
    entry_cooldown_seconds: int


class EffectivePolicy(BaseModel):
    """The compiled policy actually used by signal, risk and execution code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = SCHEMA_VERSION
    policy_id: UUID
    status: PolicyStatus = PolicyStatus.ACTIVE
    generated_at: AwareDatetime
    expires_at: AwareDatetime
    compiled_at: AwareDatetime
    mode_scope: list[Mode]
    regime: Regime
    universe: list[EffectiveSymbolPolicy]
    portfolio: PortfolioPolicy
    jev_gate: JevGatePolicy
    exit_defaults: ExitDefaults
    invalidation: list[str]
    rationale: str
    clamps: list[Clamp]
    policy_hash: str
    raw_policy: StrategyPolicy

    def symbol_policy(self, symbol: str) -> EffectiveSymbolPolicy | None:
        for item in self.universe:
            if item.symbol == symbol:
                return item
        return None

    def applies_to(self, mode: Mode) -> bool:
        if mode is Mode.SHADOW:
            return Mode.PAPER in self.mode_scope or Mode.TESTNET in self.mode_scope
        return mode in self.mode_scope

    def is_fresh(self, now: datetime | None = None) -> bool:
        return (now or utcnow()) < self.expires_at


def _policy_covers_mode(policy: StrategyPolicy, mode: Mode) -> bool:
    if mode is Mode.SHADOW:
        return Mode.PAPER in policy.mode_scope or Mode.TESTNET in policy.mode_scope
    return mode in policy.mode_scope


class PolicyCompiler:
    """Validate and clamp an untrusted model policy against server limits."""

    def __init__(self, server_limits: ServerLimits) -> None:
        self.server = server_limits

    def compile(
        self,
        policy: StrategyPolicy,
        *,
        now: datetime | None = None,
        mode: Mode = Mode.PAPER,
    ) -> EffectivePolicy:
        now = now or utcnow()
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        clamps: list[Clamp] = []

        if policy.generated_at > now + timedelta(seconds=60):
            raise PolicyInvalidError(
                "Policy generated_at is in the future beyond allowed clock skew.",
                details={
                    "generated_at": policy.generated_at.isoformat(),
                    "now": now.isoformat(),
                },
            )
        if policy.expires_at <= now:
            raise PolicyExpiredError(
                details={"expires_at": policy.expires_at.isoformat(), "now": now.isoformat()}
            )
        if not _policy_covers_mode(policy, mode):
            raise PolicyInvalidError(
                f"Policy mode_scope does not include {mode.value}.",
                details={"mode_scope": [m.value for m in policy.mode_scope]},
            )

        max_expiry = now + timedelta(seconds=self.server.max_policy_ttl_seconds)
        effective_expiry = policy.expires_at
        if effective_expiry > max_expiry:
            clamps.append(
                Clamp(
                    field="expires_at",
                    requested=policy.expires_at.isoformat(),
                    effective=max_expiry.isoformat(),
                    reason="server max policy TTL",
                )
            )
            effective_expiry = max_expiry

        universe = self._compile_universe(policy, clamps)
        portfolio = self._clamp_portfolio(policy.portfolio, clamps)
        jev_gate = self._clamp_gate(policy.jev_gate, clamps)
        exit_defaults = self._clamp_exits(policy.exit_defaults, clamps)

        return EffectivePolicy(
            policy_id=policy.policy_id,
            status=PolicyStatus.ACTIVE,
            generated_at=policy.generated_at,
            expires_at=effective_expiry,
            compiled_at=now,
            mode_scope=policy.mode_scope,
            regime=policy.regime,
            universe=universe,
            portfolio=portfolio,
            jev_gate=jev_gate,
            exit_defaults=exit_defaults,
            invalidation=policy.invalidation,
            rationale=policy.rationale,
            clamps=clamps,
            policy_hash=policy.policy_hash(),
            raw_policy=policy,
        )

    def _compile_universe(
        self, policy: StrategyPolicy, clamps: list[Clamp]
    ) -> list[EffectiveSymbolPolicy]:
        universe: list[EffectiveSymbolPolicy] = []
        for proposed in policy.universe:
            if proposed.symbol not in self.server.allowed_symbols:
                clamps.append(
                    Clamp(
                        field=f"universe.{proposed.symbol}",
                        requested="present",
                        effective="removed",
                        reason="symbol not in server allowlist",
                    )
                )
                continue

            sides = [s for s in proposed.allowed_sides if s in self.server.allowed_entry_sides]
            if len(sides) != len(proposed.allowed_sides):
                clamps.append(
                    Clamp(
                        field=f"universe.{proposed.symbol}.allowed_sides",
                        requested=",".join(s.value for s in proposed.allowed_sides),
                        effective=",".join(s.value for s in sides),
                        reason="spot long-only entry envelope",
                    )
                )
            if not sides:
                continue

            max_notional = min(
                proposed.max_position_notional_usdt,
                self.server.max_position_notional_usdt,
            )
            if max_notional != proposed.max_position_notional_usdt:
                clamps.append(
                    Clamp(
                        field=f"universe.{proposed.symbol}.max_position_notional_usdt",
                        requested=str(proposed.max_position_notional_usdt),
                        effective=str(max_notional),
                        reason="server max position notional",
                    )
                )

            max_spread = min(proposed.max_spread_bps, self.server.max_spread_bps)
            if max_spread != proposed.max_spread_bps:
                clamps.append(
                    Clamp(
                        field=f"universe.{proposed.symbol}.max_spread_bps",
                        requested=str(proposed.max_spread_bps),
                        effective=str(max_spread),
                        reason="server max spread",
                    )
                )

            max_slippage = min(proposed.max_slippage_bps, self.server.max_slippage_bps)
            if max_slippage != proposed.max_slippage_bps:
                clamps.append(
                    Clamp(
                        field=f"universe.{proposed.symbol}.max_slippage_bps",
                        requested=str(proposed.max_slippage_bps),
                        effective=str(max_slippage),
                        reason="server max slippage",
                    )
                )

            cooldown = max(
                proposed.entry_cooldown_seconds,
                self.server.min_entry_cooldown_seconds,
            )
            if cooldown != proposed.entry_cooldown_seconds:
                clamps.append(
                    Clamp(
                        field=f"universe.{proposed.symbol}.entry_cooldown_seconds",
                        requested=str(proposed.entry_cooldown_seconds),
                        effective=str(cooldown),
                        reason="server minimum entry cooldown",
                    )
                )

            universe.append(
                EffectiveSymbolPolicy(
                    symbol=proposed.symbol,
                    allowed_sides=sides,
                    max_position_notional_usdt=max_notional,
                    max_spread_bps=max_spread,
                    max_slippage_bps=max_slippage,
                    entry_cooldown_seconds=cooldown,
                )
            )

        if not universe:
            raise PolicyInvalidError(
                "Policy has no tradable symbol after applying the server envelope."
            )
        return universe

    def _clamp_portfolio(
        self, proposed: PortfolioPolicy, clamps: list[Clamp]
    ) -> PortfolioPolicy:
        fields: dict[str, tuple[Decimal, Decimal]] = {
            "max_gross_exposure_pct": (
                proposed.max_gross_exposure_pct,
                self.server.max_gross_exposure_pct,
            ),
            "max_daily_loss_pct": (
                proposed.max_daily_loss_pct,
                self.server.max_daily_loss_pct,
            ),
            "max_drawdown_pct": (
                proposed.max_drawdown_pct,
                self.server.max_drawdown_pct,
            ),
            "max_positions": (
                Decimal(proposed.max_positions),
                Decimal(self.server.max_positions),
            ),
        }
        effective: dict[str, Decimal] = {}
        for field, (requested, hard) in fields.items():
            value = min(requested, hard)
            if value != requested:
                clamps.append(
                    Clamp(
                        field=f"portfolio.{field}",
                        requested=str(requested),
                        effective=str(value),
                        reason="server portfolio limit",
                    )
                )
            effective[field] = value

        return PortfolioPolicy(
            max_gross_exposure_pct=effective["max_gross_exposure_pct"],
            max_daily_loss_pct=effective["max_daily_loss_pct"],
            max_drawdown_pct=effective["max_drawdown_pct"],
            max_positions=int(effective["max_positions"]),
        )

    def _clamp_gate(self, proposed: JevGatePolicy, clamps: list[Clamp]) -> JevGatePolicy:
        probability = max(proposed.min_choice_probability, self.server.min_choice_probability)
        fit = max(proposed.min_policy_fit, self.server.min_policy_fit)
        quality = max(proposed.min_quality_score, self.server.min_quality_score)

        for field, requested, value in (
            ("min_choice_probability", proposed.min_choice_probability, probability),
            ("min_policy_fit", proposed.min_policy_fit, fit),
            ("min_quality_score", proposed.min_quality_score, quality),
        ):
            if value != requested:
                clamps.append(
                    Clamp(
                        field=f"jev_gate.{field}",
                        requested=str(requested),
                        effective=str(value),
                        reason="server minimum gate threshold",
                    )
                )

        return JevGatePolicy(
            entry_choice=proposed.entry_choice,
            min_choice_probability=probability,
            min_policy_fit=fit,
            min_quality_score=quality,
        )

    def _clamp_exits(self, proposed: ExitDefaults, clamps: list[Clamp]) -> ExitDefaults:
        # The ratchets are opt-in, so a policy that leaves them out keeps the
        # exact v1 geometry. When present they are bounded like every other
        # exit value: the server can narrow them, never widen them.
        breakeven = proposed.breakeven_trigger_bps
        if breakeven is not None and breakeven < self.server.min_stop_loss_bps:
            clamps.append(
                Clamp(
                    field="exit_defaults.breakeven_trigger_bps",
                    requested=str(breakeven),
                    effective=str(self.server.min_stop_loss_bps),
                    reason="break-even must not arm inside the minimum stop distance",
                )
            )
            breakeven = self.server.min_stop_loss_bps

        trailing_trigger = proposed.trailing_trigger_bps
        trailing_distance = proposed.trailing_distance_bps
        if trailing_distance is not None and trailing_distance > self.server.max_trailing_distance_bps:
            clamps.append(
                Clamp(
                    field="exit_defaults.trailing_distance_bps",
                    requested=str(trailing_distance),
                    effective=str(self.server.max_trailing_distance_bps),
                    reason="server trailing distance bound",
                )
            )
            trailing_distance = self.server.max_trailing_distance_bps
        # Narrowing the distance can never break coherence: the validator
        # already requires distance < trigger, and clamping only lowers the
        # distance. The server therefore cannot produce the pair the validator
        # refuses, and needs no fallback branch for it.

        stop_loss = min(
            max(proposed.stop_loss_bps, self.server.min_stop_loss_bps),
            self.server.max_stop_loss_bps,
        )
        if stop_loss != proposed.stop_loss_bps:
            clamps.append(
                Clamp(
                    field="exit_defaults.stop_loss_bps",
                    requested=str(proposed.stop_loss_bps),
                    effective=str(stop_loss),
                    reason="server stop-loss bounds",
                )
            )

        take_profit = min(proposed.take_profit_bps, self.server.max_take_profit_bps)
        if take_profit != proposed.take_profit_bps:
            clamps.append(
                Clamp(
                    field="exit_defaults.take_profit_bps",
                    requested=str(proposed.take_profit_bps),
                    effective=str(take_profit),
                    reason="server take-profit bound",
                )
            )

        holding = min(proposed.max_holding_seconds, self.server.max_holding_seconds)
        if holding != proposed.max_holding_seconds:
            clamps.append(
                Clamp(
                    field="exit_defaults.max_holding_seconds",
                    requested=str(proposed.max_holding_seconds),
                    effective=str(holding),
                    reason="server max holding time",
                )
            )

        return ExitDefaults(
            stop_loss_bps=stop_loss,
            take_profit_bps=take_profit,
            max_holding_seconds=holding,
            breakeven_trigger_bps=breakeven,
            trailing_trigger_bps=trailing_trigger,
            trailing_distance_bps=trailing_distance,
        )
