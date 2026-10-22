"""Govern how often a policy may be replaced: refresh TTL and minimum life.

A policy's TTL decides how soon the runtime looks at the market again, and it
has both failure directions:

- **Too long** and the model's read goes stale; this is what the server
  ceiling (``max_policy_ttl_seconds``) already bounds.
- **Too short** and the runtime stops reviewing and starts churning. In
  production every policy lived for two hours on a one-hour cycle, so each
  regeneration discarded a regime read before a second candle could test it.
  That matters beyond talk: ``policy_fit`` is scored *against a policy*, so a
  new policy resets the baseline the score is measured on, and a buy rejected
  under one policy is not comparable with one rejected under the next.

So the TTL now has a floor and a ceiling, and it is no longer the model's
choice inside that band: the runtime sets it.

``min_policy_life_seconds`` also decides when the runtime may regenerate at
all. A policy is refreshed when it is *about to* expire -- within one refresh
lead of it -- rather than only after it has expired, so the replacement is in
place before the old one lapses instead of leaving a window with no policy.
That is why the floor is a floor: anything shorter than the lead would mean
the policy is due for replacement the moment it is created.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

#: Default floor for a policy's lifetime. Six hours is six candles on a
#: one-hour cycle: long enough for a policy to be tested by more than the
#: read it was written from.
DEFAULT_MIN_POLICY_LIFE_SECONDS = 6 * 3600
#: How early the runtime replaces a policy before it lapses. A refresh that
#: fails must leave the old policy working, not leave none.
DEFAULT_POLICY_REFRESH_LEAD_SECONDS = 1800


@dataclass(frozen=True)
class PolicyLife:
    """The band a policy's lifetime must fall in, and when to refresh it."""

    min_seconds: int = DEFAULT_MIN_POLICY_LIFE_SECONDS
    refresh_lead_seconds: int = DEFAULT_POLICY_REFRESH_LEAD_SECONDS

    def __post_init__(self) -> None:
        if self.min_seconds <= 0:
            raise ValueError("min_policy_life_seconds must be positive")
        if not 0 < self.refresh_lead_seconds < self.min_seconds:
            raise ValueError(
                "policy_refresh_lead_seconds must be shorter than "
                "min_policy_life_seconds, or every new policy is due at once"
            )

    @property
    def min_life(self) -> timedelta:
        return timedelta(seconds=self.min_seconds)

    @property
    def refresh_lead(self) -> timedelta:
        return timedelta(seconds=self.refresh_lead_seconds)

    def lifetime(self, max_ttl_seconds: int) -> timedelta:
        """How long a freshly compiled policy lives.

        The floor wins over the ceiling only if the server is configured with a
        ceiling below its own floor; that is a configuration error, and the
        stricter of the two (the ceiling) is what actually holds.
        """

        return min(self.min_life, timedelta(seconds=max_ttl_seconds))

    def due_for_refresh(self, *, expires_at: datetime, now: datetime) -> bool:
        """Whether the active policy should be replaced to have one ready."""

        return expires_at - now <= self.refresh_lead


def effective_expiry(*, requested, generated_at, now, min_life, max_ttl):
    """The expiry a policy gets, and whether either bound moved it.

    The floor is a lifetime measured from ``generated_at``, like the policy's
    own expiry; the ceiling is a lifetime from ``now``, because a policy that
    is compiled late must not get a later deadline. The ceiling is applied
    last: it is the server invariant and holds whatever the policy asked.

    Returns ``(expiry, raised_to_floor, cut_to_ceiling)``.
    """

    floor = generated_at + min_life
    ceiling = now + max_ttl
    expiry = max(requested, floor)
    # Both bounds are reported against the final value, so the clamps
    # recorded always describe where the policy actually ends up. A policy
    # that the floor pushed past the ceiling is cut by the ceiling and says
    # so, even though it never asked for more than the ceiling itself.
    cut = expiry > ceiling
    if cut:
        expiry = ceiling
    raised = expiry > requested
    return expiry, raised, cut
