"""Deterministic risk engine.

This is the only component allowed to approve execution.  It never calls a
model and never trusts an intent's claimed values.  All economically
significant arithmetic is Decimal-only.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .enums import IntentType, Mode, OrderPreference, Side
from .intent import ApprovedTradeIntent, TradeIntent
from .market import AccountSnapshot, MarketSnapshot, SymbolFilters, SystemSnapshot
from .money import ZERO, canonical_str
from .policy import EffectivePolicy, sha256_json


class RuleFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule: str
    message: str
    details: dict[str, str] = Field(default_factory=dict)


class RiskContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy: EffectivePolicy
    account: AccountSnapshot
    market: MarketSnapshot
    symbol_rules: SymbolFilters
    system: SystemSnapshot
    now: AwareDatetime

    position_qty: Decimal = ZERO
    last_entry_at: datetime | None = None
    duplicate_intent: bool = False

    max_staleness_ms: int = Field(default=5000, ge=0)
    exit_slippage_bps: Decimal = Field(default=Decimal("8"), ge=0)
    max_intent_ttl_seconds: int = Field(default=30, gt=0)


class RiskDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: UUID = Field(default_factory=uuid4)
    intent_id: UUID
    approved: bool
    failed_rules: list[RuleFailure] = Field(default_factory=list)
    effective: dict[str, str] = Field(default_factory=dict)
    snapshot_hash: str
    decision_hash: str
    created_at: AwareDatetime
    approved_intent: ApprovedTradeIntent | None = None

    @property
    def failed_rule_names(self) -> list[str]:
        return [failure.rule for failure in self.failed_rules]


def _canonical_decimal(value: Decimal | None) -> str | None:
    return canonical_str(value) if value is not None else None


def compute_risk_snapshot_hash(intent: TradeIntent, ctx: RiskContext) -> str:
    """Hash the exact state the decision depends on.

    The signal engine and the risk engine both use this helper, so a stale
    or tampered intent cannot be approved against a different snapshot.
    """
    intent_payload = intent.model_dump(mode="json")
    intent_payload.pop("risk_snapshot_hash", None)
    payload = {
        "intent": intent_payload,
        "policy_hash": ctx.policy.policy_hash,
        "account": {
            "equity_usdt": canonical_str(ctx.account.equity_usdt),
            "available_quote_usdt": canonical_str(ctx.account.available_quote_usdt),
            "gross_exposure_usdt": canonical_str(ctx.account.gross_exposure_usdt),
            "open_positions": ctx.account.open_positions,
            "daily_realized_pnl_pct": canonical_str(ctx.account.daily_realized_pnl_pct),
            "drawdown_pct": canonical_str(ctx.account.drawdown_pct),
        },
        "market": {
            "snapshot_id": str(ctx.market.snapshot_id),
            "symbol": ctx.market.symbol,
            "as_of": ctx.market.as_of.isoformat(),
            "bid": canonical_str(ctx.market.bid),
            "ask": canonical_str(ctx.market.ask),
            "spread_bps": canonical_str(ctx.market.spread_bps),
        },
        "rules": {
            "symbol": ctx.symbol_rules.symbol,
            "status": ctx.symbol_rules.status,
            "step_size": canonical_str(ctx.symbol_rules.step_size),
            "min_qty": canonical_str(ctx.symbol_rules.min_qty),
            "min_notional": canonical_str(ctx.symbol_rules.min_notional),
            "tick_size": canonical_str(ctx.symbol_rules.tick_size),
        },
        "system": {
            "mode": ctx.system.mode,
            "live_armed": ctx.system.live_armed,
            "kill_switch": ctx.system.kill_switch,
            "feed_healthy": ctx.system.feed_healthy,
            "execution_healthy": ctx.system.execution_healthy,
        },
        "position_qty": canonical_str(ctx.position_qty),
    }
    return sha256_json(payload)


class RiskEngine:
    """Evaluate an intent against server/policy/account/exchange limits."""

    def evaluate(self, intent: TradeIntent, ctx: RiskContext) -> RiskDecision:
        now = ctx.now
        policy = ctx.policy
        market = ctx.market
        account = ctx.account
        rules = ctx.symbol_rules
        symbol_policy = policy.symbol_policy(intent.symbol)
        failures: list[RuleFailure] = []

        snapshot_hash = compute_risk_snapshot_hash(intent, ctx)
        if intent.risk_snapshot_hash != snapshot_hash:
            failures.append(
                RuleFailure(
                    rule="risk_snapshot_mismatch",
                    message="Intent risk snapshot hash does not match current state.",
                )
            )

        self._check_time_mode_health(intent, ctx, failures)
        self._check_symbol_policy(intent, ctx, symbol_policy, failures)
        effective_notional = self._effective_notional(intent, ctx, symbol_policy, failures)
        approved_intent = None
        if not failures:
            approved_intent, build_failures = self._build_approved(
                intent, ctx, symbol_policy, effective_notional
            )
            failures.extend(build_failures)
            if build_failures:
                approved_intent = None

        effective = {
            "quote_notional": canonical_str(effective_notional),
            "max_position_notional_usdt": (
                canonical_str(symbol_policy.max_position_notional_usdt) if symbol_policy else ""
            ),
            "max_gross_exposure_pct": canonical_str(policy.portfolio.max_gross_exposure_pct),
            "max_positions": str(policy.portfolio.max_positions),
            "stop_loss_bps": (
                canonical_str(policy.exit_defaults.stop_loss_bps)
                if intent.intent_type is IntentType.OPEN
                else ""
            ),
            "take_profit_bps": (
                canonical_str(policy.exit_defaults.take_profit_bps)
                if intent.intent_type is IntentType.OPEN
                else ""
            ),
        }
        if approved_intent is not None:
            effective["approved_qty"] = canonical_str(approved_intent.approved_qty)
            effective["approved_limit_price"] = _canonical_decimal(
                approved_intent.approved_limit_price
            ) or ""

        approved = not failures and approved_intent is not None
        decision_id = uuid4()
        decision_hash = sha256_json(
            {
                "intent_id": str(intent.intent_id),
                "snapshot_hash": snapshot_hash,
                "approved": approved,
                "failed_rules": [f.model_dump(mode="json") for f in failures],
                "effective": effective,
                "approved_intent": (
                    approved_intent.model_dump(mode="json") if approved_intent else None
                ),
            }
        )
        if approved_intent is not None:
            approved_intent = approved_intent.model_copy(
                update={"risk_decision_id": decision_id, "decision_hash": decision_hash}
            )
        return RiskDecision(
            decision_id=decision_id,
            intent_id=intent.intent_id,
            approved=approved,
            failed_rules=failures,
            effective=effective,
            snapshot_hash=snapshot_hash,
            decision_hash=decision_hash,
            created_at=now,
            approved_intent=approved_intent,
        )

    def _check_time_mode_health(
        self, intent: TradeIntent, ctx: RiskContext, failures: list[RuleFailure]
    ) -> None:
        now = ctx.now
        if now >= intent.expires_at:
            failures.append(RuleFailure(rule="intent_expired", message="TradeIntent has expired."))
        if (intent.expires_at - intent.created_at).total_seconds() > ctx.max_intent_ttl_seconds:
            failures.append(
                RuleFailure(rule="intent_ttl", message="TradeIntent TTL exceeds server maximum.")
            )
        is_entry = intent.intent_type is IntentType.OPEN
        if is_entry and not ctx.policy.is_fresh(now):
            failures.append(
                RuleFailure(rule="policy_expired", message="Strategy policy has expired.")
            )
        if is_entry and not ctx.policy.applies_to(intent.mode):
            failures.append(
                RuleFailure(
                    rule="policy_mode_scope",
                    message="Policy does not cover this execution mode.",
                )
            )
        if is_entry and intent.policy_id != ctx.policy.policy_id:
            failures.append(
                RuleFailure(rule="policy_mismatch", message="Intent policy_id is not active.")
            )
        # Entries must match the server mode. Exits may carry the mode that owns
        # the position (live resets to paper on restart, but the exchange
        # position still has to be closed on the live route).
        if (
            is_entry
            and intent.mode is not Mode.SHADOW
            and intent.mode.value != ctx.system.mode
        ):
            failures.append(
                RuleFailure(
                    rule="mode_mismatch",
                    message="Intent mode does not match the server mode.",
                    details={"intent": intent.mode.value, "server": ctx.system.mode},
                )
            )
        if is_entry and intent.mode is Mode.LIVE and not ctx.system.live_armed:
            failures.append(
                RuleFailure(rule="live_not_armed", message="Live mode is not armed.")
            )
        if is_entry and ctx.system.kill_switch:
            failures.append(RuleFailure(rule="kill_switch", message="Kill switch is active."))
        if is_entry and not ctx.system.feed_healthy:
            failures.append(
                RuleFailure(rule="feed_unhealthy", message="Market feed is unhealthy.")
            )
        if not ctx.system.execution_healthy:
            failures.append(
                RuleFailure(
                    rule="execution_unhealthy", message="Execution dependency is unhealthy."
                )
            )
        if is_entry and not ctx.market.is_fresh(now, ctx.max_staleness_ms):
            failures.append(
                RuleFailure(
                    rule="stale_market_data",
                    message="Market snapshot is stale.",
                    details={"staleness_ms": str(ctx.market.staleness_ms(now))},
                )
            )
        if ctx.duplicate_intent:
            failures.append(
                RuleFailure(
                    rule="idempotency_conflict",
                    message="An intent with this idempotency key already exists.",
                )
            )

    def _check_symbol_policy(
        self,
        intent: TradeIntent,
        ctx: RiskContext,
        symbol_policy,
        failures: list[RuleFailure],
    ) -> None:
        policy = ctx.policy
        rules = ctx.symbol_rules
        if intent.intent_type is IntentType.OPEN and symbol_policy is None:
            failures.append(
                RuleFailure(
                    rule="symbol_not_in_policy",
                    message="Symbol is not in the effective policy universe.",
                    details={"symbol": intent.symbol},
                )
            )
        if rules.status != "TRADING":
            failures.append(
                RuleFailure(
                    rule="symbol_not_trading",
                    message="Exchange reports the symbol as not tradable.",
                    details={"status": rules.status},
                )
            )
        if symbol_policy is not None and intent.intent_type is IntentType.OPEN:
            if ctx.market.spread_bps > symbol_policy.max_spread_bps:
                failures.append(
                    RuleFailure(
                        rule="spread_above_policy",
                        message="Market spread exceeds the policy limit.",
                        details={
                            "spread_bps": canonical_str(ctx.market.spread_bps),
                            "max_spread_bps": canonical_str(symbol_policy.max_spread_bps),
                        },
                    )
                )
            if intent.max_slippage_bps > symbol_policy.max_slippage_bps:
                failures.append(
                    RuleFailure(
                        rule="slippage_above_policy",
                        message="Intent slippage tolerance exceeds the policy limit.",
                        details={
                            "max_slippage_bps": canonical_str(intent.max_slippage_bps),
                            "policy_limit": canonical_str(symbol_policy.max_slippage_bps),
                        },
                    )
                )

        if intent.intent_type is IntentType.OPEN:
            if symbol_policy is not None and intent.side not in symbol_policy.allowed_sides:
                failures.append(
                    RuleFailure(
                        rule="side_not_allowed",
                        message="Entry side is not allowed by the policy.",
                        details={"side": intent.side.value},
                    )
                )
            if intent.side is Side.SELL:
                failures.append(
                    RuleFailure(
                        rule="spot_long_only",
                        message="Spot entry SELL is blocked; SELL is reserved for exits.",
                    )
                )
            if ctx.position_qty > ZERO:
                failures.append(
                    RuleFailure(
                        rule="position_already_open",
                        message="A position already exists for this symbol (no pyramiding).",
                    )
                )
            if ctx.account.daily_realized_pnl_pct <= -policy.portfolio.max_daily_loss_pct:
                failures.append(
                    RuleFailure(
                        rule="daily_loss_limit",
                        message="Daily realized loss limit has been reached.",
                    )
                )
            if ctx.account.drawdown_pct >= policy.portfolio.max_drawdown_pct:
                failures.append(
                    RuleFailure(
                        rule="max_drawdown",
                        message="Maximum drawdown limit has been reached.",
                    )
                )
            if ctx.account.open_positions >= policy.portfolio.max_positions:
                failures.append(
                    RuleFailure(
                        rule="max_positions",
                        message="Maximum number of open positions reached.",
                        details={
                            "open_positions": str(ctx.account.open_positions),
                            "max_positions": str(policy.portfolio.max_positions),
                        },
                    )
                )
            if symbol_policy is not None and ctx.last_entry_at is not None:
                cooldown_until = (
                    ctx.last_entry_at.timestamp() + symbol_policy.entry_cooldown_seconds
                )
                if ctx.now.timestamp() < cooldown_until:
                    failures.append(
                        RuleFailure(
                            rule="entry_cooldown",
                            message="Entry cooldown is still active for this symbol.",
                        )
                    )
        else:
            if ctx.position_qty <= ZERO:
                failures.append(
                    RuleFailure(rule="no_position", message="Cannot close a non-existent position.")
                )
            if intent.side is not Side.SELL:
                failures.append(
                    RuleFailure(rule="close_side", message="A spot close must be a SELL.")
                )

    def _reference_price(self, intent: TradeIntent, ctx: RiskContext) -> Decimal:
        if intent.limit_price is not None:
            return intent.limit_price
        return ctx.market.ask if intent.side is Side.BUY else ctx.market.bid

    def _effective_notional(
        self,
        intent: TradeIntent,
        ctx: RiskContext,
        symbol_policy,
        failures: list[RuleFailure],
    ) -> Decimal:
        rules = ctx.symbol_rules
        account = ctx.account
        fee_rate = rules.taker_fee_bps / Decimal("10000")
        reference_price = self._reference_price(intent, ctx)

        if intent.intent_type is IntentType.CLOSE:
            if ctx.position_qty <= ZERO:
                return ZERO
            exit_price = ctx.market.bid * (Decimal("1") - ctx.exit_slippage_bps / Decimal("10000"))
            return max(ZERO, ctx.position_qty * exit_price)

        effective = intent.quote_notional
        if symbol_policy is not None:
            effective = min(effective, symbol_policy.max_position_notional_usdt)

        equity = account.equity_usdt
        if equity <= ZERO:
            failures.append(
                RuleFailure(rule="zero_equity", message="Account equity is not positive.")
            )
            return ZERO
        max_gross = equity * ctx.policy.portfolio.max_gross_exposure_pct / Decimal("100")
        remaining = max(ZERO, max_gross - account.gross_exposure_usdt)
        if effective > remaining:
            effective = remaining
        if effective <= ZERO:
            failures.append(
                RuleFailure(
                    rule="max_gross_exposure",
                    message="No gross exposure headroom remains for a new entry.",
                )
            )
            return ZERO

        affordable = account.available_quote_usdt / (Decimal("1") + fee_rate)
        if effective > affordable:
            effective = affordable
        if effective <= ZERO or effective < rules.min_notional:
            failures.append(
                RuleFailure(
                    rule="minimum_notional",
                    message="Effective notional is below the exchange minimum.",
                    details={
                        "effective": canonical_str(effective),
                        "min_notional": canonical_str(rules.min_notional),
                    },
                )
            )
            return max(ZERO, effective)

        return effective

    def _build_approved(
        self,
        intent: TradeIntent,
        ctx: RiskContext,
        symbol_policy,
        effective_notional: Decimal,
    ) -> tuple[ApprovedTradeIntent | None, list[RuleFailure]]:
        failures: list[RuleFailure] = []
        rules = ctx.symbol_rules
        policy = ctx.policy

        raw_price = self._reference_price(intent, ctx)
        price = rules.quantize_price(raw_price)
        if price <= ZERO:
            failures.append(
                RuleFailure(rule="invalid_price", message="Limit price rounds to zero.")
            )
            return None, failures
        price_problems = rules.validate_price(price)
        for problem in price_problems:
            failures.append(RuleFailure(rule="price_filter", message=problem))

        if intent.intent_type is IntentType.CLOSE:
            qty = rules.quantize_qty(ctx.position_qty)
            notional = qty * ctx.market.bid
        elif intent.order_preference is OrderPreference.MARKET:
            slippage_rate = intent.max_slippage_bps / Decimal("10000")
            fill_reference = ctx.market.ask * (Decimal("1") + slippage_rate)
            qty = rules.quantize_qty(effective_notional / fill_reference)
            notional = qty * fill_reference
        else:
            qty = rules.quantize_qty(effective_notional / price)
            notional = qty * price

        if qty <= ZERO:
            failures.append(
                RuleFailure(
                    rule="quantity_rounds_to_zero",
                    message="Quantity rounds to zero at the exchange step size.",
                )
            )
        else:
            for problem in rules.validate_qty(qty):
                failures.append(RuleFailure(rule="quantity_filter", message=problem))
            if not rules.meets_min_notional(qty, price):
                failures.append(
                    RuleFailure(
                        rule="minimum_notional",
                        message="Order quantity does not meet MIN_NOTIONAL.",
                        details={
                            "notional": canonical_str(qty * price),
                            "min_notional": canonical_str(rules.min_notional),
                        },
                    )
                )

        expected_fee = notional * rules.taker_fee_bps / Decimal("10000")
        if intent.intent_type is IntentType.OPEN:
            fee_rate = rules.taker_fee_bps / Decimal("10000")
            if notional + expected_fee > ctx.account.available_quote_usdt:
                failures.append(
                    RuleFailure(
                        rule="insufficient_balance",
                        message="Order notional plus fees exceeds available quote balance.",
                        details={
                            "required": canonical_str(notional + expected_fee),
                            "available": canonical_str(ctx.account.available_quote_usdt),
                        },
                    )
                )

            stop_bps = policy.exit_defaults.stop_loss_bps
            adverse_rate = (
                stop_bps + rules.taker_fee_bps + ctx.exit_slippage_bps
            ) / Decimal("10000")
            exit_price = price * (Decimal("1") - adverse_rate)
            if exit_price <= ZERO:
                failures.append(
                    RuleFailure(
                        rule="REJECT_EXIT_MIN_NOTIONAL",
                        message="Adverse exit price is non-positive under the stop-loss.",
                    )
                )
            elif qty < rules.min_qty:
                failures.append(
                    RuleFailure(
                        rule="REJECT_EXIT_MIN_NOTIONAL",
                        message="Position cannot be legally sold after a stop-loss move.",
                        details={
                            "exit_qty": canonical_str(qty),
                            "min_qty": canonical_str(rules.min_qty),
                        },
                    )
                )
            else:
                exit_notional = qty * exit_price
                if exit_notional < rules.min_notional:
                    failures.append(
                        RuleFailure(
                            rule="REJECT_EXIT_MIN_NOTIONAL",
                            message="Position cannot be legally sold after a stop-loss move.",
                            details={
                                "exit_notional": canonical_str(exit_notional),
                                "min_notional": canonical_str(rules.min_notional),
                                "stop_loss_bps": canonical_str(stop_bps),
                                "exit_slippage_bps": canonical_str(ctx.exit_slippage_bps),
                            },
                        )
                    )

        if failures:
            return None, failures

        approved = ApprovedTradeIntent(
            intent=intent,
            risk_decision_id=uuid4(),
            decision_hash="pending",
            approved_at=ctx.now,
            effective_quote_notional=notional,
            approved_qty=qty,
            approved_limit_price=price if intent.order_preference is OrderPreference.LIMIT else None,
            effective_stop_loss_bps=(
                policy.exit_defaults.stop_loss_bps
                if intent.intent_type is IntentType.OPEN
                else None
            ),
            effective_take_profit_bps=(
                policy.exit_defaults.take_profit_bps
                if intent.intent_type is IntentType.OPEN
                else None
            ),
            effective_max_holding_seconds=(
                policy.exit_defaults.max_holding_seconds
                if intent.intent_type is IntentType.OPEN
                else None
            ),
            expected_fee_quote=expected_fee,
        )
        return approved, []
