"""SQLAlchemy persistence models.

PostgreSQL is the authoritative production store. SQLite is a local-dev
fallback with the same schema. Economically significant values use
Numeric/Decimal columns; raw provider payloads use JSON.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_uuid() -> str:
    return str(uuid4())


class DecimalText(TypeDecorator):
    """Exact decimal storage.

    PostgreSQL uses NUMERIC. SQLite has no decimal type and would otherwise
    round-trip money through binary floats, so the same value is stored as
    its canonical decimal string there. Application code always sees Decimal.
    """

    impl = Numeric(28, 12)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(64))
        return dialect.type_descriptor(Numeric(28, 12))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        decimal_value = Decimal(str(value))
        if dialect.name == "sqlite":
            return format(decimal_value.normalize(), "f")
        return decimal_value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return Decimal(str(value))


class JSONText(TypeDecorator):
    """JSON on PostgreSQL, a JSON string on SQLite.

    SQLAlchemy's generic ``JSON`` does not reliably round-trip nested Python
    values on SQLite (instances can load as raw strings). Agent traces are
    lists of dicts, so they get a decorator that is explicit about both.
    """

    impl = Text()
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(Text())
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "sqlite":
            return json.dumps(value, ensure_ascii=False, default=str)
        return value

    def process_result_value(self, value, dialect):
        if value is None or dialect.name != "sqlite":
            return value
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value


MONEY = DecimalText()
QTY = DecimalText()
PCT = DecimalText()


class UTCDateTime(TypeDecorator):
    """Store UTC and always return timezone-aware UTC datetimes.

    SQLite drops tzinfo; without this decorator every comparison between a
    persisted datetime and ``datetime.now(UTC)`` would fail.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class SchemaMigrationRow(Base):
    """Applied schema migrations. Each version runs exactly once per database."""

    __tablename__ = "schema_migrations"

    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    checksum: Mapped[str] = mapped_column(String(64))
    applied_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AccountRow(Base):
    __tablename__ = "trading_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    mode: Mapped[str] = mapped_column(String(16))
    starting_equity_usdt: Mapped[Decimal] = mapped_column(MONEY)
    cash_quote_usdt: Mapped[Decimal] = mapped_column(MONEY)
    realized_pnl_usdt: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    peak_equity_usdt: Mapped[Decimal] = mapped_column(MONEY)
    daily_realized_pnl_usdt: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    daily_pnl_date: Mapped[str] = mapped_column(String(10), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )


class ModelConnectionRow(Base):
    __tablename__ = "model_connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    provider: Mapped[str] = mapped_column(String(32))
    purpose: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    base_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("provider", "purpose", name="uq_connection_provider_purpose"),)


class StrategyPolicyRow(Base):
    __tablename__ = "strategy_policies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    policy_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    source: Mapped[str] = mapped_column(String(32), default="unknown")
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_json: Mapped[dict] = mapped_column(JSON)
    effective_json: Mapped[dict] = mapped_column(JSON)
    mode_scope_json: Mapped[list] = mapped_column(JSON)
    clamps_json: Mapped[list] = mapped_column(JSON)
    policy_hash: Mapped[str] = mapped_column(String(80), index=True)
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AgentRunRow(Base):
    """One strategy-agent attempt: what it looked at and why it decided.

    The journal is operator-facing and intentionally separate from the audit
    chain: tool traces can be large and are not part of the hash-linked
    economic record. The resulting policy still lands in ``strategy_policies``
    and the audit trail as before.
    """

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    cycle_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    policy_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    mode: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="ok")
    turns: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls_json: Mapped[list] = mapped_column(JSONText, default=list)
    usage_json: Mapped[dict] = mapped_column(JSONText, default=dict)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class MarketSnapshotRow(Base):
    __tablename__ = "market_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    as_of: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    source: Mapped[str] = mapped_column(String(32))
    bid: Mapped[Decimal] = mapped_column(MONEY)
    ask: Mapped[Decimal] = mapped_column(MONEY)
    spread_bps: Mapped[Decimal] = mapped_column(PCT)
    candle_close_at: Mapped[datetime] = mapped_column(UTCDateTime)
    features_json: Mapped[dict] = mapped_column(JSON)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class JevEvaluationRow(Base):
    __tablename__ = "jev_evaluations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    signal_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("signals.id"), nullable=True, index=True
    )
    model: Mapped[str] = mapped_column(String(64))
    request_json: Mapped[dict] = mapped_column(JSON)
    response_json: Mapped[dict] = mapped_column(JSON, default=dict)
    answers_json: Mapped[dict] = mapped_column(JSON, default=dict)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SignalRow(Base):
    __tablename__ = "signals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    policy_id: Mapped[str] = mapped_column(String(36), index=True)
    snapshot_id: Mapped[str] = mapped_column(String(36), index=True)
    jev_evaluation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    side: Mapped[str] = mapped_column(String(8))
    score_json: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (Index("ix_signals_state_created", "state", "created_at"),)


class SignalEventRow(Base):
    __tablename__ = "signal_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    signal_id: Mapped[str] = mapped_column(String(36), ForeignKey("signals.id"), index=True)
    state: Mapped[str] = mapped_column(String(32))
    detail_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class CounterfactualRow(Base):
    """What a blocked buy would have returned under the exit rules in force.

    Rejected signals never reach the order path, so the only outcomes the
    system could otherwise observe are the ones the gate let through. One row
    per blocked signal, written once the full holding window has elapsed; the
    notional is a replay assumption, never a real position.
    """

    __tablename__ = "counterfactual_outcomes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    signal_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("signals.id"), unique=True, index=True
    )
    policy_id: Mapped[str] = mapped_column(String(36), index=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    entry_choice: Mapped[str] = mapped_column(String(16))
    entry_probability: Mapped[float] = mapped_column(Float)
    policy_fit: Mapped[float] = mapped_column(Float)
    quality_score: Mapped[float] = mapped_column(Float)
    blocked_rule: Mapped[str] = mapped_column(String(32))
    assumed_notional_usdt: Mapped[Decimal] = mapped_column(MONEY)
    entry_price: Mapped[Decimal] = mapped_column(MONEY)
    exit_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    exit_reason: Mapped[str] = mapped_column(String(24))
    net_bps: Mapped[Decimal | None] = mapped_column(PCT, nullable=True)
    pnl_usdt: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    horizon_seconds: Mapped[int] = mapped_column(Integer)
    # Price path over the whole window, in bps against the entry price:
    # how far it ever went in favour (MFE) and against (MAE).
    mfe_bps: Mapped[Decimal | None] = mapped_column(PCT, nullable=True)
    mae_bps: Mapped[Decimal | None] = mapped_column(PCT, nullable=True)
    # Seconds from the signal to the bar that set MFE / MAE, so "price never
    # paid" can be told apart from "price paid and we gave it back".
    time_to_mfe_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    time_to_mae_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Round-trip cost charged to this replay (both fees + exit slippage). Stored
    # rather than re-derived: the fee schedule in force is part of the record.
    round_trip_cost_bps: Mapped[Decimal | None] = mapped_column(PCT, nullable=True)
    holding_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Minutes with no 1m bar inside the window. A large gap means the replay
    # saw less of the path than it claims to have judged.
    gap_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    method: Mapped[str] = mapped_column(String(48))
    evaluated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class EdgeShadowRow(Base):
    """What the shadow edge evaluator said about one signal, and why.

    Written once per signal at decision time, whether or not enough past
    outcomes existed to say anything. Storing the uninformative verdicts is
    the point: "we had no evidence yet" becomes a cohort that can be counted,
    rather than a gap in the record that looks like agreement.
    """

    __tablename__ = "edge_shadow_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    signal_id: Mapped[str] = mapped_column(String(36), index=True)
    cohort: Mapped[str] = mapped_column(String(64), index=True)
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(48))
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    expected_net_bps: Mapped[Decimal | None] = mapped_column(PCT, nullable=True)
    lower_ci_bps: Mapped[Decimal | None] = mapped_column(PCT, nullable=True)
    upper_ci_bps: Mapped[Decimal | None] = mapped_column(PCT, nullable=True)
    required_net_bps: Mapped[Decimal | None] = mapped_column(PCT, nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(PCT, nullable=True)
    estimator: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class TradeIntentRow(Base):
    __tablename__ = "trade_intents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    signal_id: Mapped[str] = mapped_column(String(36), ForeignKey("signals.id"), index=True)
    policy_id: Mapped[str] = mapped_column(String(36), index=True)
    account_id: Mapped[str] = mapped_column(String(36), index=True)
    mode: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    side: Mapped[str] = mapped_column(String(8))
    intent_type: Mapped[str] = mapped_column(String(8))
    order_preference: Mapped[str] = mapped_column(String(8))
    quote_notional: Mapped[Decimal] = mapped_column(MONEY)
    max_slippage_bps: Mapped[Decimal] = mapped_column(PCT)
    limit_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    stop_loss_bps: Mapped[Decimal | None] = mapped_column(PCT, nullable=True)
    take_profit_bps: Mapped[Decimal | None] = mapped_column(PCT, nullable=True)
    max_holding_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="RISK_PENDING", index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    risk_snapshot_hash: Mapped[str] = mapped_column(String(80))
    provenance_json: Mapped[dict] = mapped_column(JSON, default=dict)


class RiskDecisionRow(Base):
    __tablename__ = "risk_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    intent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("trade_intents.id"), index=True
    )
    approved: Mapped[bool] = mapped_column(Boolean, index=True)
    failed_rules_json: Mapped[list] = mapped_column(JSON, default=list)
    effective_json: Mapped[dict] = mapped_column(JSON, default=dict)
    snapshot_hash: Mapped[str] = mapped_column(String(80))
    decision_hash: Mapped[str] = mapped_column(String(80))
    approved_intent_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    intent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("trade_intents.id"), index=True
    )
    exchange_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(8))
    qty: Mapped[Decimal] = mapped_column(QTY)
    price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    filled_qty: Mapped[Decimal] = mapped_column(QTY, default=Decimal("0"))
    avg_fill_price: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    fee_quote: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    mode: Mapped[str] = mapped_column(String(16))
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )


class OrderEventRow(Base):
    __tablename__ = "order_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    order_id: Mapped[str] = mapped_column(String(36), ForeignKey("orders.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class FillRow(Base):
    __tablename__ = "fills"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    order_id: Mapped[str] = mapped_column(String(36), ForeignKey("orders.id"), index=True)
    # Exchange trade id when the fill came from (or was reconciled with) a venue.
    external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    qty: Mapped[Decimal] = mapped_column(QTY)
    price: Mapped[Decimal] = mapped_column(MONEY)
    fee_quote: Mapped[Decimal] = mapped_column(MONEY)
    fee_asset: Mapped[str] = mapped_column(String(16), default="USDT")
    # The commission exactly as the venue charged it, in ``fee_asset``.
    # ``fee_quote`` is its value in the quote asset; the two differ for a spot
    # buy, whose fee is taken out of the base asset. Keeping both means a fee
    # that cannot be priced (BNB) is visible as a number instead of being
    # rounded to zero in the ledger.
    fee_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("order_id", "external_id", name="uq_fills_order_external"),
    )


class PositionRow(Base):
    __tablename__ = "positions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    account_id: Mapped[str] = mapped_column(String(36), index=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    qty: Mapped[Decimal] = mapped_column(QTY)
    avg_entry_price: Mapped[Decimal] = mapped_column(MONEY)
    stop_loss_bps: Mapped[Decimal] = mapped_column(PCT)
    take_profit_bps: Mapped[Decimal] = mapped_column(PCT)
    max_holding_seconds: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), index=True, default="OPEN")
    realized_pnl_usdt: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    entry_order_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    exit_order_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    policy_id: Mapped[str] = mapped_column(String(36))
    signal_id: Mapped[str] = mapped_column(String(36))
    # Execution route that owns this position. Exits must go back through the
    # same adapter even after a restart or a mode change.
    mode: Mapped[str] = mapped_column(String(16), default="paper")
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    # Set when an operator asks for an immediate close. The exit manager reads
    # it as an exit reason, so a failed attempt stays visible until it succeeds.
    exit_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    exit_requested_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)


class PnlSnapshotRow(Base):
    __tablename__ = "pnl_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    account_id: Mapped[str] = mapped_column(String(36), index=True)
    as_of: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    equity_usdt: Mapped[Decimal] = mapped_column(MONEY)
    cash_quote_usdt: Mapped[Decimal] = mapped_column(MONEY)
    realized_pnl_usdt: Mapped[Decimal] = mapped_column(MONEY)
    unrealized_pnl_usdt: Mapped[Decimal] = mapped_column(MONEY)
    gross_exposure_usdt: Mapped[Decimal] = mapped_column(MONEY)
    daily_realized_pnl_usdt: Mapped[Decimal] = mapped_column(MONEY)
    drawdown_pct: Mapped[Decimal] = mapped_column(PCT)


class BalanceSnapshotRow(Base):
    """One exchange-account view on one route, as the venue reported it."""

    __tablename__ = "balance_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    source: Mapped[str] = mapped_column(String(32), index=True)
    asset: Mapped[str] = mapped_column(String(16))
    free: Mapped[Decimal] = mapped_column(MONEY)
    locked: Mapped[Decimal] = mapped_column(MONEY)
    total: Mapped[Decimal] = mapped_column(MONEY)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class AuditEventRow(Base):
    __tablename__ = "audit_events"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(36), unique=True, default=new_uuid)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str] = mapped_column(String(32), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(80))
    event_hash: Mapped[str] = mapped_column(String(80), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SystemStateRow(Base):
    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_json: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )


class BacktestRunRow(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    status: Mapped[str] = mapped_column(String(24), index=True)
    request_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )


class ArmingTokenRow(Base):
    __tablename__ = "arming_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    preflight_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
