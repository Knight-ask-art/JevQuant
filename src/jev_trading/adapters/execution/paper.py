"""Paper execution adapter.

Paper and Live consume the same ApprovedTradeIntent.  Paper adds exchange
filters, spread-aware fills, fees, deterministic partial fills and the same
positions/PnL projection.  It is not a separate toy business-logic path.
"""

from __future__ import annotations

import random
from decimal import Decimal
from uuid import uuid4

from ...domain.enums import IntentType, OrderPreference, OrderStatus
from ...domain.intent import ApprovedTradeIntent
from ...domain.market import MarketSnapshot, SymbolFilters
from ...domain.money import ZERO, canonical_str, quantize_down
from ...storage.repository import ExecutionFill, Repository
from .base import ExecutionReceipt, ReconciliationResult


class PaperExecutionAdapter:
    name = "paper"

    def __init__(
        self,
        repo: Repository,
        *,
        seed: int = 42,
        partial_fill_probability: float = 0.0,
        partial_fill_ratio: Decimal = Decimal("0.5"),
    ) -> None:
        self.repo = repo
        self.seed = seed
        self.partial_fill_probability = partial_fill_probability
        self.partial_fill_ratio = partial_fill_ratio

    async def submit(
        self,
        approved: ApprovedTradeIntent,
        market: MarketSnapshot,
        rules: SymbolFilters,
    ) -> ExecutionReceipt:
        intent = approved.intent
        order_id = str(uuid4())
        rng = random.Random(f"{self.seed}:{intent.intent_id}")
        fee_bps = rules.taker_fee_bps

        if intent.order_preference is OrderPreference.LIMIT:
            limit = approved.approved_limit_price or market.ask
            if intent.side.value == "buy":
                if market.ask > limit:
                    await self._record_resting(approved, order_id)
                    return ExecutionReceipt(
                        order_id=order_id,
                        status=OrderStatus.ACCEPTED,
                        message="Limit buy rested below the ask (paper).",
                    )
                fill_price = min(limit, market.ask)
            else:
                if market.bid < limit:
                    await self._record_resting(approved, order_id)
                    return ExecutionReceipt(
                        order_id=order_id,
                        status=OrderStatus.ACCEPTED,
                        message="Limit sell rested above the bid (paper).",
                    )
                fill_price = max(limit, market.bid)
        else:
            fill_price = market.ask if intent.side.value == "buy" else market.bid

        fill_price = rules.quantize_price(fill_price)
        qty = approved.approved_qty
        if intent.intent_type is IntentType.OPEN and rng.random() < self.partial_fill_probability:
            qty = quantize_down(qty * self.partial_fill_ratio, rules.step_size)

        if qty <= ZERO:
            return ExecutionReceipt(
                order_id=order_id,
                status=OrderStatus.REJECTED,
                message="Paper qty rounded to zero; order not filled.",
            )

        fee = qty * fill_price * fee_bps / Decimal("10000")
        fills = [ExecutionFill(qty=qty, price=fill_price, fee_quote=fee)]
        status = (
            OrderStatus.FILLED
            if qty >= approved.approved_qty
            else OrderStatus.PARTIALLY_FILLED
        )
        order, position = await self.repo.record_execution(
            approved=approved,
            order_id=order_id,
            exchange_order_id=f"paper-{order_id[:8]}",
            status=status,
            fills=fills,
            account_id=str(intent.account_id),
        )
        await self.repo.update_intent_status(
            str(intent.intent_id), status.value
        )
        return ExecutionReceipt(
            order_id=order.id,
            exchange_order_id=order.exchange_order_id,
            status=status,
            filled_qty=qty,
            avg_fill_price=fill_price,
            fee_quote=fee,
            position_id=position.id if position is not None else None,
            message=f"paper fill {canonical_str(qty)} @ {canonical_str(fill_price)}",
        )

    async def _record_resting(self, approved: ApprovedTradeIntent, order_id: str) -> None:
        await self.repo.record_execution(
            approved=approved,
            order_id=order_id,
            exchange_order_id=f"paper-{order_id[:8]}",
            status=OrderStatus.ACCEPTED,
            fills=[],
            account_id=str(approved.intent.account_id),
        )
        await self.repo.update_intent_status(
            str(approved.intent.intent_id), OrderStatus.ACCEPTED.value
        )

    async def cancel(self, order_id: str) -> None:
        # Paper orders have no exchange-side state: a cancel is final at once,
        # so the order row must stop counting as unsettled.
        await self.repo.record_order_event(order_id, "CANCEL_REQUESTED", {"backend": self.name})
        await self.repo.update_order_status(
            order_id, OrderStatus.CANCELLED, note="paper cancel is immediate"
        )

    async def reconcile(self, account_id: str) -> ReconciliationResult:
        return ReconciliationResult(
            healthy=True,
            notes=["Paper backend has no external order state."],
        )

    async def health(self) -> dict[str, object]:
        return {"healthy": True, "backend": self.name, "simulated": True}

    async def close(self) -> None:
        return None
