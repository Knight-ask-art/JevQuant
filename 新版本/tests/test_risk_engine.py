"""Deterministic risk gates: the only path to execution."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from conftest import (
    NOW,
    account_snapshot,
    compiled_policy,
    make_context,
    make_intent,
    market_snapshot,
    symbol_rules,
    system_snapshot,
)
from jev_trading.domain.enums import IntentType, Mode, Side
from jev_trading.domain.risk import RiskEngine

ENGINE = RiskEngine()


def rules(decision) -> set[str]:
    return set(decision.failed_rule_names)


def test_clean_entry_is_approved_within_envelope(server_limits):
    policy = compiled_policy(server_limits)
    ctx = make_context(policy)
    decision = ENGINE.evaluate(make_intent(policy, ctx), ctx)
    assert decision.approved, decision.failed_rules
    approved = decision.approved_intent
    assert approved is not None
    assert approved.effective_quote_notional <= Decimal("500")
    assert approved.approved_qty > 0
    assert approved.expected_fee_quote > 0
    assert decision.decision_hash.startswith("sha256:")


def test_notional_is_clamped_to_policy_cap(server_limits):
    policy = compiled_policy(server_limits)
    ctx = make_context(policy)
    decision = ENGINE.evaluate(make_intent(policy, ctx, quote_notional="900"), ctx)
    assert decision.approved
    assert decision.approved_intent.effective_quote_notional <= Decimal("500")


def test_existing_position_blocks_pyramiding(server_limits):
    policy = compiled_policy(server_limits)
    ctx = make_context(policy, position_qty="0.001")
    decision = ENGINE.evaluate(make_intent(policy, ctx), ctx)
    assert not decision.approved
    assert "position_already_open" in rules(decision)


def test_max_positions_blocks_entry(server_limits):
    policy = compiled_policy(server_limits, max_positions=1)
    ctx = make_context(policy, account=account_snapshot(open_positions=1))
    decision = ENGINE.evaluate(make_intent(policy, ctx), ctx)
    assert "max_positions" in rules(decision)


def test_daily_loss_limit_blocks_entry(server_limits):
    policy = compiled_policy(server_limits)
    account = account_snapshot()
    account = account.model_copy(update={"daily_realized_pnl_pct": Decimal("-2.5")})
    ctx = make_context(policy, account=account)
    decision = ENGINE.evaluate(make_intent(policy, ctx), ctx)
    assert "daily_loss_limit" in rules(decision)


def test_stale_feed_blocks_entry_but_never_blocks_exit(server_limits):
    policy = compiled_policy(server_limits)
    stale = market_snapshot(NOW, stale_ms=60_000)
    entry_ctx = make_context(policy, market=stale)
    entry = ENGINE.evaluate(make_intent(policy, entry_ctx), entry_ctx)
    assert "stale_market_data" in rules(entry)

    exit_ctx = make_context(policy, market=stale, position_qty="0.001")
    exit_intent = make_intent(
        policy,
        exit_ctx,
        intent_type=IntentType.CLOSE,
        side=Side.SELL,
        limit_price="67990",
    )
    exit_decision = ENGINE.evaluate(exit_intent, exit_ctx)
    assert exit_decision.approved, exit_decision.failed_rules


def test_kill_switch_blocks_entry_but_not_exit(server_limits):
    policy = compiled_policy(server_limits)
    system = system_snapshot(kill=True)
    entry_ctx = make_context(policy, system=system)
    assert "kill_switch" in rules(ENGINE.evaluate(make_intent(policy, entry_ctx), entry_ctx))

    exit_ctx = make_context(policy, system=system, position_qty="0.001")
    exit_intent = make_intent(
        policy,
        exit_ctx,
        intent_type=IntentType.CLOSE,
        side=Side.SELL,
        limit_price="67990",
    )
    assert ENGINE.evaluate(exit_intent, exit_ctx).approved


def test_exit_feasibility_precheck_rejects_unbuyable_position(server_limits):
    policy = compiled_policy(server_limits)
    constrained = symbol_rules(min_notional="99")
    ctx = make_context(policy, rules=constrained)
    decision = ENGINE.evaluate(make_intent(policy, ctx, quote_notional="100"), ctx)
    assert not decision.approved
    assert "REJECT_EXIT_MIN_NOTIONAL" in rules(decision)


def test_stop_limit_must_survive_the_configured_stress_drawdown(server_limits):
    policy = compiled_policy(server_limits)
    ctx = make_context(policy, rules=symbol_rules(min_notional="50"))
    # 100 USDT buys ~0.00147 BTC; after -3% - 10bps - 8bps it is worth ~97,
    # which clears MIN_NOTIONAL 50, so the entry is approved.
    decision = ENGINE.evaluate(make_intent(policy, ctx, quote_notional="100"), ctx)
    assert decision.approved, decision.failed_rules

    # Raise the stress move until the sellable remainder cannot clear
    # MIN_NOTIONAL any more.
    hard = make_context(
        policy,
        rules=symbol_rules(min_notional="95"),
        exit_stress_drawdown_bps=Decimal("600"),
    )
    blocked = ENGINE.evaluate(make_intent(policy, hard, quote_notional="100"), hard)
    assert not blocked.approved
    assert "REJECT_EXIT_MIN_NOTIONAL" in rules(blocked)
    detail = next(f for f in blocked.failed_rules if f.rule == "REJECT_EXIT_MIN_NOTIONAL")
    assert detail.details["exit_stress_drawdown_bps"] == "600"


def test_exit_stress_never_tightens_below_the_stop(server_limits):
    """A stop wider than the stress move still assumes the stop's own move."""

    policy = compiled_policy(server_limits)
    ctx = make_context(
        policy,
        rules=symbol_rules(min_notional="90"),
        exit_stress_drawdown_bps=Decimal("0"),
    )
    decision = ENGINE.evaluate(make_intent(policy, ctx, quote_notional="100"), ctx)
    # The policy's stop-loss default is wider than 0bps, so the adverse move
    # is the stop, not the (disabled) stress: 100 * (1 - stop - 10 - 8 bps).
    assert decision.approved, decision.failed_rules


def test_exit_stress_is_part_of_the_risk_snapshot_hash(server_limits):
    policy = compiled_policy(server_limits)
    base = make_context(policy)
    stressed = make_context(policy, exit_stress_drawdown_bps=Decimal("500"))
    intent = make_intent(policy, base)
    # The same intent cannot be approved against a context it was not built for.
    decision = ENGINE.evaluate(intent, stressed)
    assert "risk_snapshot_mismatch" in rules(decision)


def test_spread_above_policy_is_rejected(server_limits):
    policy = compiled_policy(server_limits)
    wide = market_snapshot(NOW, bid="67900", ask="68100")
    ctx = make_context(policy, market=wide)
    decision = ENGINE.evaluate(make_intent(policy, ctx), ctx)
    assert "spread_above_policy" in rules(decision)


def test_non_trading_symbol_is_rejected(server_limits):
    policy = compiled_policy(server_limits)
    ctx = make_context(policy, rules=symbol_rules(status="BREAK"))
    decision = ENGINE.evaluate(make_intent(policy, ctx), ctx)
    assert "symbol_not_trading" in rules(decision)


def test_mode_mismatch_is_rejected(server_limits):
    policy = compiled_policy(server_limits)
    ctx = make_context(policy, system=system_snapshot(mode=Mode.TESTNET))
    decision = ENGINE.evaluate(make_intent(policy, ctx), ctx)
    assert "mode_mismatch" in rules(decision)


def test_tampered_snapshot_hash_is_rejected(server_limits):
    policy = compiled_policy(server_limits)
    ctx = make_context(policy)
    intent = make_intent(policy, ctx).model_copy(
        update={"risk_snapshot_hash": "sha256:deadbeef"}
    )
    decision = ENGINE.evaluate(intent, ctx)
    assert "risk_snapshot_mismatch" in rules(decision)


def test_expired_intent_is_rejected(server_limits):
    policy = compiled_policy(server_limits)
    ctx = make_context(policy)
    intent = make_intent(
        policy, ctx, created_at=NOW - timedelta(seconds=30), ttl_seconds=10
    )
    decision = ENGINE.evaluate(intent, ctx)
    assert "intent_expired" in rules(decision)


def test_duplicate_intent_is_rejected(server_limits):
    policy = compiled_policy(server_limits)
    ctx = make_context(policy, duplicate=True)
    decision = ENGINE.evaluate(make_intent(policy, ctx), ctx)
    assert "idempotency_conflict" in rules(decision)


def test_close_without_position_is_rejected(server_limits):
    policy = compiled_policy(server_limits)
    ctx = make_context(policy)
    intent = make_intent(
        policy,
        ctx,
        intent_type=IntentType.CLOSE,
        side=Side.SELL,
        limit_price="67990",
    )
    decision = ENGINE.evaluate(intent, ctx)
    assert "no_position" in rules(decision)
