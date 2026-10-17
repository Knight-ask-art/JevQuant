"""Shadow execution sink: real data/models/risk, no exchange order."""

from __future__ import annotations

from uuid import uuid4

from ...domain.enums import OrderStatus
from ...domain.intent import ApprovedTradeIntent
from ...domain.market import MarketSnapshot, SymbolFilters
from ...storage.repository import Repository
from .base import ExecutionReceipt, ReconciliationResult


class ShadowExecutionAdapter:
    name = "shadow"

    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    async def submit(
        self,
        approved: ApprovedTradeIntent,
        market: MarketSnapshot,
        rules: SymbolFilters,
    ) -> ExecutionReceipt:
        order_id = str(uuid4())
        order, _ = await self.repo.record_execution(
            approved=approved,
            order_id=order_id,
            exchange_order_id=f"shadow-{order_id[:8]}",
            status=OrderStatus.SHADOWED,
            fills=[],
            account_id=str(approved.intent.account_id),
        )
        await self.repo.update_intent_status(str(approved.intent.intent_id), "SHADOWED")
        return ExecutionReceipt(
            order_id=order.id,
            exchange_order_id=order.exchange_order_id,
            status=OrderStatus.SHADOWED,
            message="shadow: would have submitted, no exchange order placed",
        )

    async def cancel(self, order_id: str) -> None:
        await self.repo.record_order_event(order_id, "SHADOW_CANCEL_NOOP")
        await self.repo.update_order_status(
            order_id, OrderStatus.CANCELLED, note="shadow cancel is local only"
        )

    async def reconcile(self, account_id: str) -> ReconciliationResult:
        return ReconciliationResult(healthy=True, notes=["Shadow sink has no external state."])

    async def health(self) -> dict[str, object]:
        return {"healthy": True, "backend": self.name, "simulated": True}

    async def close(self) -> None:
        return None
