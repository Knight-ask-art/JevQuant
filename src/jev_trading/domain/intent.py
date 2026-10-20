"""The immutable TradeIntent contract shared by Paper, Testnet and Live.

Models may reason about an opportunity, but only this object can cross the
risk boundary.  Once approved it is frozen and carries every value the
execution adapter needs.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from .enums import IntentType, Mode, OrderPreference, Side

IntentId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9:_-]{8,128}$")]


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    llm_model: str | None = None
    jev_model: str | None = None
    jev_evaluation_id: str | None = None
    policy_source: str = "unknown"
    data_source: str = "unknown"
    simulated: bool = False


class TradeIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "trade-intent/1.0"
    intent_id: UUID
    signal_id: UUID
    policy_id: UUID
    mode: Mode
    account_id: UUID
    symbol: str
    side: Side
    intent_type: IntentType
    order_preference: OrderPreference

    quote_notional: Decimal = Field(gt=0)
    max_slippage_bps: Decimal = Field(ge=0)
    limit_price: Decimal | None = Field(default=None, gt=0)

    stop_loss_bps: Decimal | None = Field(default=None, gt=0)
    take_profit_bps: Decimal | None = Field(default=None, gt=0)
    max_holding_seconds: int | None = Field(default=None, gt=0)

    created_at: AwareDatetime
    expires_at: AwareDatetime
    idempotency_key: IntentId
    risk_snapshot_hash: str
    provenance: Provenance

    @model_validator(mode="after")
    def _validate_shape(self) -> TradeIntent:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        ttl_ms = (self.expires_at - self.created_at).total_seconds() * 1000
        if ttl_ms > 60_000:
            raise ValueError("intent TTL exceeds the 60 second hard maximum")
        if self.order_preference is OrderPreference.LIMIT and self.limit_price is None:
            raise ValueError("LIMIT intent requires limit_price")
        if self.order_preference is OrderPreference.MARKET and self.limit_price is not None:
            raise ValueError("MARKET intent must not carry limit_price")
        if self.intent_type is IntentType.OPEN:
            if self.stop_loss_bps is None or self.take_profit_bps is None:
                raise ValueError("OPEN intent requires stop_loss_bps and take_profit_bps")
            if self.max_holding_seconds is None:
                raise ValueError("OPEN intent requires max_holding_seconds")
        return self


class ApprovedTradeIntent(BaseModel):
    """Frozen execution contract produced only by the risk engine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: TradeIntent
    risk_decision_id: UUID
    decision_hash: str
    approved_at: AwareDatetime
    effective_quote_notional: Decimal = Field(gt=0)
    approved_qty: Decimal = Field(gt=0)
    approved_limit_price: Decimal | None = Field(default=None, gt=0)
    effective_stop_loss_bps: Decimal | None = Field(default=None, gt=0)
    effective_take_profit_bps: Decimal | None = Field(default=None, gt=0)
    effective_max_holding_seconds: int | None = Field(default=None, gt=0)
    expected_fee_quote: Decimal = Field(default=Decimal("0"), ge=0)

    @property
    def intent_id(self) -> UUID:
        return self.intent.intent_id

    @property
    def symbol(self) -> str:
        return self.intent.symbol

    @property
    def side(self) -> Side:
        return self.intent.side
