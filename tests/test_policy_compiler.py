"""The compiler may only narrow a model proposal, never widen it."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from conftest import NOW, raw_policy
from jev_trading.domain.enums import Mode
from jev_trading.domain.errors import PolicyInvalidError
from jev_trading.domain.policy import PolicyCompiler


def test_server_envelope_narrows_every_field(server_limits):
    policy = raw_policy(
        NOW,
        notional="1000",
        spread="20",
        slippage="20",
        cooldown=0,
        max_positions=10,
        expires_in=900,
    )
    effective = PolicyCompiler(server_limits).compile(policy, now=NOW, mode=Mode.PAPER)

    symbol = effective.symbol_policy("BTCUSDT")
    assert symbol is not None
    assert symbol.max_position_notional_usdt == server_limits.max_position_notional_usdt
    assert symbol.max_spread_bps == server_limits.max_spread_bps
    assert symbol.max_slippage_bps == server_limits.max_slippage_bps
    assert symbol.entry_cooldown_seconds == server_limits.min_entry_cooldown_seconds
    assert effective.portfolio.max_positions == server_limits.max_positions
    fields = {clamp.field for clamp in effective.clamps}
    assert "universe.BTCUSDT.max_position_notional_usdt" in fields
    assert "portfolio.max_positions" in fields


def test_policy_cannot_widen_beyond_server_limits(server_limits):
    weak = raw_policy(
        NOW,
        notional="10",
        spread="1",
        slippage="1",
        cooldown=server_limits.min_entry_cooldown_seconds,
        max_positions=1,
    )
    effective = PolicyCompiler(server_limits).compile(weak, now=NOW)
    symbol = effective.symbol_policy("BTCUSDT")
    assert symbol.max_position_notional_usdt == Decimal("10")
    assert symbol.max_spread_bps == Decimal("1")
    assert symbol.max_slippage_bps == Decimal("1")
    assert effective.portfolio.max_positions == 1
    assert effective.clamps == []


def test_expiry_is_clamped_to_server_ttl(server_limits):
    policy = raw_policy(NOW, expires_in=86_000)
    effective = PolicyCompiler(server_limits).compile(policy, now=NOW)
    ttl = (effective.expires_at - NOW).total_seconds()
    assert ttl <= server_limits.max_policy_ttl_seconds
    assert any(clamp.field == "expires_at" for clamp in effective.clamps)


def test_unknown_symbols_are_removed(server_limits):
    policy = raw_policy(NOW, symbols=("BTCUSDT", "DOGEUSDT"))
    effective = PolicyCompiler(server_limits).compile(policy, now=NOW)
    assert [item.symbol for item in effective.universe] == ["BTCUSDT"]
    assert any("DOGEUSDT" in clamp.field for clamp in effective.clamps)


def test_policy_with_only_unknown_symbols_is_rejected(server_limits):
    policy = raw_policy(NOW, symbols=("DOGEUSDT",))
    with pytest.raises(PolicyInvalidError):
        PolicyCompiler(server_limits).compile(policy, now=NOW)


def test_mode_scope_must_cover_the_target_mode(server_limits):
    policy = raw_policy(NOW, mode_scope=(Mode.TESTNET,))
    with pytest.raises(PolicyInvalidError):
        PolicyCompiler(server_limits).compile(policy, now=NOW, mode=Mode.PAPER)


def test_shadow_mode_is_covered_by_paper_scope(server_limits):
    policy = raw_policy(NOW, mode_scope=(Mode.PAPER,))
    effective = PolicyCompiler(server_limits).compile(policy, now=NOW, mode=Mode.SHADOW)
    assert effective.applies_to(Mode.SHADOW)


def test_expired_policy_is_rejected(server_limits):
    policy = raw_policy(NOW)
    later = NOW + timedelta(seconds=2_000)
    with pytest.raises(Exception):
        PolicyCompiler(server_limits).compile(policy, now=later)


def test_policy_hash_is_stable(server_limits):
    policy = raw_policy(NOW)
    first = PolicyCompiler(server_limits).compile(policy, now=NOW)
    second = PolicyCompiler(server_limits).compile(policy, now=NOW)
    assert first.policy_hash == second.policy_hash
    assert first.policy_hash.startswith("sha256:")
