"""Enumerations shared across the trading domain."""

from __future__ import annotations

from enum import Enum


class Mode(str, Enum):
    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"
    SHADOW = "shadow"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class IntentType(str, Enum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"


class OrderPreference(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class SignalState(str, Enum):
    CANDIDATE = "CANDIDATE"
    JEV_EVALUATING = "JEV_EVALUATING"
    SCORED = "SCORED"
    REJECTED = "REJECTED"
    ELIGIBLE = "ELIGIBLE"
    INTENT_CREATED = "INTENT_CREATED"
    RISK_PENDING = "RISK_PENDING"
    RISK_REJECTED = "RISK_REJECTED"
    APPROVED = "APPROVED"
    EXECUTION_PENDING = "EXECUTION_PENDING"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    POSITION_OPEN = "POSITION_OPEN"
    EXIT_PENDING = "EXIT_PENDING"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"
    ERROR = "ERROR"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


_SIGNAL_TRANSITIONS: dict[SignalState, frozenset[SignalState]] = {
    SignalState.CANDIDATE: frozenset({SignalState.JEV_EVALUATING, SignalState.EXPIRED}),
    # EXPIRED: the call was never made (budget spent, symbol halted), so there
    # is no answer to score and nothing was paid for.
    SignalState.JEV_EVALUATING: frozenset(
        {SignalState.SCORED, SignalState.ERROR, SignalState.EXPIRED}
    ),
    SignalState.SCORED: frozenset({SignalState.REJECTED, SignalState.ELIGIBLE}),
    SignalState.ELIGIBLE: frozenset({SignalState.INTENT_CREATED, SignalState.EXPIRED}),
    SignalState.INTENT_CREATED: frozenset({SignalState.RISK_PENDING, SignalState.EXPIRED}),
    SignalState.RISK_PENDING: frozenset({SignalState.RISK_REJECTED, SignalState.APPROVED}),
    SignalState.APPROVED: frozenset({SignalState.EXECUTION_PENDING, SignalState.EXPIRED}),
    SignalState.EXECUTION_PENDING: frozenset(
        {SignalState.ACCEPTED, SignalState.RECONCILIATION_REQUIRED, SignalState.ERROR}
    ),
    SignalState.ACCEPTED: frozenset(
        {SignalState.PARTIALLY_FILLED, SignalState.FILLED, SignalState.CANCELLED}
    ),
    SignalState.PARTIALLY_FILLED: frozenset(
        {SignalState.FILLED, SignalState.CANCELLED, SignalState.RECONCILIATION_REQUIRED}
    ),
    SignalState.FILLED: frozenset({SignalState.POSITION_OPEN}),
    SignalState.POSITION_OPEN: frozenset({SignalState.EXIT_PENDING}),
    SignalState.EXIT_PENDING: frozenset(
        {
            SignalState.CLOSED,
            SignalState.RECONCILIATION_REQUIRED,
            SignalState.ERROR,
            SignalState.POSITION_OPEN,
        }
    ),
    SignalState.RECONCILIATION_REQUIRED: frozenset(
        {SignalState.ACCEPTED, SignalState.FILLED, SignalState.ERROR}
    ),
    SignalState.REJECTED: frozenset(),
    SignalState.RISK_REJECTED: frozenset(),
    SignalState.CANCELLED: frozenset(),
    SignalState.CLOSED: frozenset(),
    SignalState.EXPIRED: frozenset(),
    SignalState.ERROR: frozenset(),
}

TERMINAL_SIGNAL_STATES = frozenset(
    {
        SignalState.REJECTED,
        SignalState.RISK_REJECTED,
        SignalState.CANCELLED,
        SignalState.CLOSED,
        SignalState.EXPIRED,
        SignalState.ERROR,
    }
)


def assert_signal_transition(current: SignalState, new: SignalState) -> None:
    """Raise ValueError when a lifecycle transition is not allowed."""
    if new is current:
        return
    if new not in _SIGNAL_TRANSITIONS.get(current, frozenset()):
        raise ValueError(f"illegal signal transition: {current.value} -> {new.value}")


class PolicyStatus(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"


class RegimeLabel(str, Enum):
    RISK_ON = "risk_on"
    RISK_OFF = "risk_off"
    TREND = "trend"
    RANGE = "range"
    HIGH_VOL = "high_vol"
    UNCERTAIN = "uncertain"


class JevChoice(str, Enum):
    BUY = "buy"
    WAIT = "wait"
    REJECT = "reject"


class JevQuestionType(str, Enum):
    CHOICE = "choice"
    SCORE = "score"
    NOUL = "noul"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    SHADOWED = "SHADOWED"


class PositionStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class ArmingStatus(str, Enum):
    DISARMED = "DISARMED"
    PREFLIGHT_RUNNING = "PREFLIGHT_RUNNING"
    PREFLIGHT_FAILED = "PREFLIGHT_FAILED"
    ARMED = "ARMED"


class ErrorCode(str, Enum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    PURPOSE_NOT_ALLOWED = "PURPOSE_NOT_ALLOWED"
    MODE_NOT_ARMED = "MODE_NOT_ARMED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    POLICY_EXPIRED = "POLICY_EXPIRED"
    POLICY_INVALID = "POLICY_INVALID"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    RISK_REJECTED = "RISK_REJECTED"
    LOCAL_RATE_LIMITED = "LOCAL_RATE_LIMITED"
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    DEPENDENCY_BAD_RESPONSE = "DEPENDENCY_BAD_RESPONSE"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    SAFE_STATE_ACTIVE = "SAFE_STATE_ACTIVE"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
