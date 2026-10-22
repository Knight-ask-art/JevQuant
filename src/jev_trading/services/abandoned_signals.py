"""Signals that stopped mid-pipeline, and what that leaves behind.

A signal is a saga: it moves through states written by several separate
database transactions. A process that dies between two of them — a deploy, an
OOM kill, an exception nobody caught — leaves the row in a state that no code
path will ever move again. Nothing else in the system notices: the next cycle
creates fresh signals and the stuck one sits in the table forever.

Leaving it there is not neutral. ``EXECUTION_PENDING`` counts as "reached the
market" for the pacing guard, so a stuck row makes the cycle skip a symbol it
should have asked about. And a run of them makes monitoring report work in
progress that is not happening.

The sweep runs once at startup, because the row it repairs is the one left
behind by the previous process. It also runs at the start of every cycle: a
call can hang without the process dying, and the cycle is the thing that
notices that nothing has moved since. It is not the Jev timeout — an HTTP
client timeout aborts a hung call on its own; this is the backstop for the
process itself.

This module decides which rows are abandoned. The caller performs the
transition, because a transition belongs in the repository that enforces it.

An abandoned ``EXECUTION_PENDING`` row is also the one case where the order
outcome is genuinely unknown: the adapter may have reached the venue. It is
recorded as ``RECONCILIATION_REQUIRED`` rather than as a plain failure, so the
reconcile pass — which asks the venue what happened — is what resolves it.
Closing the row never settles the order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

#: States that mean "work is in progress right now". Each one is written by a
#: transaction that is followed, in the same flow, by the next state. A row
#: that has sat in one of these for longer than any of those steps can
#: plausibly take was abandoned, not slowed down.
IN_FLIGHT_STATES: tuple[str, ...] = (
    "JEV_EVALUATING",
    "INTENT_CREATED",
    "RISK_PENDING",
    "EXECUTION_PENDING",
)

#: The state whose outcome may sit at the venue rather than in our database.
AWAITING_VENUE_STATE = "EXECUTION_PENDING"

#: How long each in-flight state may last before it counts as abandoned.
#:
#: The Jev call has its own httpx timeout, the risk evaluation is pure local
#: work, and the dispatch is bounded by the intent TTL (30 s by default).
#: Ten minutes is far above every one of those and far below a trading
#: horizon, so a healthy pipeline never trips it while a dead process always
#: does.
DEFAULT_ABANDON_AFTER = timedelta(minutes=10)

ABANDONED_REASON = "abandoned_in_flight"


def abandoned_state(state: str) -> str:
    """Where an abandoned row in ``state`` is moved to.

    The signal state machine allows ``ERROR`` and ``RECONCILIATION_REQUIRED``
    from each of the in-flight states, so neither branch invents a transition.
    """

    if state == AWAITING_VENUE_STATE:
        return "RECONCILIATION_REQUIRED"
    return "ERROR"


@dataclass(frozen=True)
class AbandonedSignal:
    """One row that will be closed, and how far it got."""

    signal_id: str
    symbol: str
    state: str
    last_seen: datetime

    @property
    def to_state(self) -> str:
        return abandoned_state(self.state)

    @property
    def awaiting_venue(self) -> bool:
        return self.state == AWAITING_VENUE_STATE

    def as_detail(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "from_state": self.state,
            "to_state": self.to_state,
            "last_seen": self.last_seen.isoformat(),
            "reason": ABANDONED_REASON,
        }


def is_abandoned(
    *,
    state: str,
    last_seen: datetime,
    now: datetime,
    abandon_after: timedelta = DEFAULT_ABANDON_AFTER,
) -> bool:
    """Whether an in-flight row has been stuck long enough to be closed."""

    if state not in IN_FLIGHT_STATES:
        return False
    # A last_seen in the future (clock skew between hosts) gives a negative
    # idle time, which fails the comparison: not evidence of abandonment.
    idle = now - last_seen
    return idle >= abandon_after


def abandoned_of(
    rows,
    *,
    now: datetime,
    abandon_after: timedelta = DEFAULT_ABANDON_AFTER,
) -> list[AbandonedSignal]:
    """Select the abandoned rows from ``(signal_id, symbol, state, events)``.

    ``last_seen`` is the newest event timestamp, so a row that moved within
    its current state recently is never abandoned on the strength of when the
    signal was created. A trail with no timestamped event cannot say when the
    row last progressed, and is left alone: guessing would close live work.
    """

    found: list[AbandonedSignal] = []
    for signal_id, symbol, state, events in rows:
        if state not in IN_FLIGHT_STATES:
            continue
        stamps = [
            event.created_at for event in (events or ()) if event.created_at is not None
        ]
        if not stamps:
            continue
        last_seen = max(stamps)
        if last_seen.tzinfo is None:
            continue
        if is_abandoned(
            state=state, last_seen=last_seen, now=now, abandon_after=abandon_after
        ):
            found.append(
                AbandonedSignal(
                    signal_id=signal_id, symbol=symbol, state=state, last_seen=last_seen
                )
            )
    return found
