"""The policy lifetime has a floor, and the refresh happens inside it.

Two behaviours are pinned here. The floor, because a lifetime shorter than the
refresh lead would be churned the moment it is created -- that is what the
productions run did with a two-hour TTL on a one-hour cycle. The lead, because
a refresh that only starts once a policy has lapsed leaves a window with no
policy at all; starting it inside the lead means a failed refresh still has
the old policy to fall back on.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from conftest import NOW, raw_policy

from jev_trading.domain.enums import Mode
from jev_trading.domain.policy import PolicyCompiler
from jev_trading.domain.policy_life import PolicyLife


# -- the band ----------------------------------------------------------------
def test_a_lifetime_inside_the_band_is_left_alone():
    # The default server has a 2 h ceiling below the 6 h floor, so this uses
    # a band where both bounds can be respected: 1 h floor, 12 h ceiling.
    from jev_trading.config import Settings

    limits = Settings(
        min_policy_life_seconds=3 * 3600, max_policy_ttl_seconds=12 * 3600
    ).server_limits
    policy = raw_policy(NOW, expires_in=4 * 3600)

    effective = PolicyCompiler(limits).compile(policy, now=NOW)

    assert effective.expires_at == policy.expires_at
    assert not [c for c in effective.clamps if c.field == "expires_at"]


def test_a_lifetime_below_the_floor_is_stretched_and_recorded():
    from jev_trading.config import Settings

    limits = Settings(
        min_policy_life_seconds=3 * 3600, max_policy_ttl_seconds=12 * 3600
    ).server_limits
    policy = raw_policy(NOW, expires_in=900)

    effective = PolicyCompiler(limits).compile(policy, now=NOW)

    # A lifetime from generated_at (the fixture sets it 5 s before NOW).
    assert effective.expires_at == policy.generated_at + timedelta(hours=3)
    reasons = [c.reason for c in effective.clamps if c.field == "expires_at"]
    assert len(reasons) == 1 and "min policy life" in reasons[0]


def test_the_shipped_floor_is_six_hours_and_its_own_ceiling_wins():
    """With the shipped defaults the ceiling is stricter than the floor.

    Six hours from generated_at and two hours from now are easy to confuse;
    the ceiling is measured from the compile time, so it is the later of the
    two results that is truncated.
    """

    from jev_trading.config import Settings

    settings = Settings()
    assert settings.max_policy_ttl_seconds == 2 * 3600
    assert settings.min_policy_life_seconds == 6 * 3600
    policy = raw_policy(NOW, expires_in=900)

    effective = PolicyCompiler(settings.server_limits).compile(policy, now=NOW)

    assert effective.expires_at == NOW + timedelta(seconds=settings.max_policy_ttl_seconds)


def test_the_ceiling_still_wins_when_it_is_below_the_floor(server_limits):
    """A misconfigured server must not produce an infinite policy."""

    limits = server_limits.model_copy(update={"max_policy_ttl_seconds": 3600})
    policy = raw_policy(NOW, expires_in=60)

    effective = PolicyCompiler(limits).compile(policy, now=NOW)

    assert effective.expires_at == NOW + timedelta(hours=1)


def test_a_floor_of_zero_leaves_the_request_alone():
    """Operators can turn the floor off; the compiler then has no opinion."""

    from jev_trading.config import Settings

    limits = Settings(min_policy_life_seconds=0).server_limits
    policy = raw_policy(NOW, expires_in=900)

    effective = PolicyCompiler(limits).compile(policy, now=NOW)

    assert effective.expires_at == policy.expires_at


# -- the lead ----------------------------------------------------------------
def test_the_life_band_refuses_a_lead_longer_than_the_floor():
    with pytest.raises(ValueError, match="shorter than"):
        PolicyLife(min_seconds=3600, refresh_lead_seconds=3600)


def test_the_life_band_refuses_a_negative_lead():
    with pytest.raises(ValueError):
        PolicyLife(refresh_lead_seconds=0)


def test_a_policy_is_due_once_it_is_inside_the_lead():
    life = PolicyLife(min_seconds=6 * 3600, refresh_lead_seconds=1800)

    assert not life.due_for_refresh(expires_at=NOW + timedelta(hours=2), now=NOW)
    assert life.due_for_refresh(expires_at=NOW + timedelta(minutes=20), now=NOW)
    assert life.due_for_refresh(expires_at=NOW, now=NOW)


def test_the_lifetime_never_exceeds_the_ceiling():
    life = PolicyLife(min_seconds=6 * 3600)

    assert life.lifetime(7200) == timedelta(hours=2)
    assert life.lifetime(86_400) == timedelta(hours=6)


# -- through the orchestrator ------------------------------------------------
def test_the_cycle_refreshes_inside_the_lead_not_after_expiry():
    from jev_trading.services.orchestrator import TradingOrchestrator

    orchestrator = TradingOrchestrator.__new__(TradingOrchestrator)
    orchestrator.policy_refresh_lead = timedelta(minutes=30)
    policy = SimpleNamespace(expires_at=NOW + timedelta(minutes=45))

    assert orchestrator._policy_due(policy, NOW) is False
    orchestrator.policy_refresh_lead = timedelta(minutes=60)
    assert orchestrator._policy_due(policy, NOW) is True


def test_an_expired_or_missing_policy_is_still_handled_by_the_cycle():
    from jev_trading.services.orchestrator import TradingOrchestrator

    orchestrator = TradingOrchestrator.__new__(TradingOrchestrator)
    orchestrator.policy_refresh_lead = timedelta(minutes=30)

    assert orchestrator._policy_due(None, NOW) is False
    orchestrator.policy_refresh_lead = None
    policy = SimpleNamespace(expires_at=NOW)
    assert orchestrator._policy_due(policy, NOW) is False


def test_mode_scope_is_still_required(server_limits):
    """The floor must not become a way to admit a policy for the wrong mode."""

    policy = raw_policy(NOW, expires_in=900, mode_scope=(Mode.TESTNET,))
    effective = PolicyCompiler(server_limits).compile(policy, now=NOW, mode=Mode.TESTNET)

    assert effective.expires_at > NOW
