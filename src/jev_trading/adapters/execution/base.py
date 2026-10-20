"""Execution gateway contract shared by every backend."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from ...domain.enums import OrderStatus
from ...domain.intent import ApprovedTradeIntent
from ...domain.market import MarketSnapshot, SymbolFilters


class ExecutionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    order_id: str
    exchange_order_id: str | None = None
    status: OrderStatus
    filled_qty: Decimal = Decimal("0")
    avg_fill_price: Decimal = Decimal("0")
    fee_quote: Decimal = Decimal("0")
    position_id: str | None = None
    reconciliation_required: bool = False
    message: str = ""


class ReconciliationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    healthy: bool
    checked_orders: int = 0
    unknown_orders: int = 0
    notes: list[str] = Field(default_factory=list)


class ExecutionAdapter(Protocol):
    name: str

    async def submit(
        self,
        approved: ApprovedTradeIntent,
        market: MarketSnapshot,
        rules: SymbolFilters,
    ) -> ExecutionReceipt: ...

    async def cancel(self, order_id: str) -> None: ...

    async def reconcile(self, account_id: str) -> ReconciliationResult: ...

    async def health(self) -> dict[str, object]: ...

    async def close(self) -> None: ...
