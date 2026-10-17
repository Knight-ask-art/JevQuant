"""SQLAlchemy persistence models.

PostgreSQL is the authoritative production store. SQLite is a local-dev
fallback with the same schema. Economically significant values use
Numeric/Decimal columns; raw provider payloads use JSON.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
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
    qty: Mapped[Decimal] = mapped_column(QTY)
    price: Mapped[Decimal] = mapped_column(MONEY)
    fee_quote: Mapped[Decimal] = mapped_column(MONEY)
    fee_asset: Mapped[str] = mapped_column(String(16), default="USDT")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


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
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False)


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
