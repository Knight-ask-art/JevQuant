"""Binance Spot Testnet execution adapter (HMAC-signed REST).

Scope: order submission with a deterministic client order id, fill parsing,
cancel and read-only order reconciliation.  It is never used for Live
funds; Live execution is routed through Hummingbot.  Any ambiguous result
becomes RECONCILIATION_REQUIRED rather than a blind retry.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import httpx

from ...domain.enums import OrderStatus
from ...domain.errors import (
    DependencyBadResponseError,
    DependencyUnavailableError,
    ProviderRateLimitedError,
    ReconciliationRequiredError,
)
from ...domain.intent import ApprovedTradeIntent
from ...domain.market import MarketSnapshot, SymbolFilters
from ...domain.money import ZERO, canonical_str
from ...storage.repository import ExecutionFill, Repository
from .base import ExecutionReceipt, ReconciliationResult

STATUS_MAP = {
    "NEW": OrderStatus.ACCEPTED,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELLED,
    "EXPIRED": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
}


def client_order_id(idempotency_key: str) -> str:
    safe = "".join(ch for ch in idempotency_key if ch.isalnum() or ch in "-_")
    return "jev-" + safe[:32]


class BinanceTestnetExecutionAdapter:
    name = "binance_testnet"

    def __init__(
        self,
        repo: Repository,
        *,
        base_url: str,
        api_key: str | None,
        api_secret: str | None,
        timeout: float = 15.0,
        recv_window: int = 5000,
    ) -> None:
        self.repo = repo
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key or ""
        self._api_secret = api_secret or ""
        self.recv_window = recv_window
        self._client = httpx.AsyncClient(timeout=timeout)
        self.configured = bool(self._api_key and self._api_secret)

    def _require_configured(self) -> None:
        if not self.configured:
            raise DependencyUnavailableError("Binance Testnet credentials are not configured.")

    def _signed_query(self, params: dict[str, Any]) -> str:
        payload = dict(params)
        payload["timestamp"] = int(time.time() * 1000)
        payload["recvWindow"] = self.recv_window
        query = urlencode(payload)
        signature = hmac.new(
            self._api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return f"{query}&signature={signature}"

    async def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self._require_configured()
        query = self._signed_query(params or {})
        url = f"{self.base_url}{path}?{query}"
        try:
            response = await self._client.request(
                method, url, headers={"X-MBX-APIKEY": self._api_key}
            )
        except httpx.HTTPError as exc:
            raise ReconciliationRequiredError(
                "Binance Testnet request outcome is unknown.",
                details={"reason": type(exc).__name__},
            ) from exc
        if response.status_code == 429:
            raise ProviderRateLimitedError("Binance Testnet rate limit reached.")
        if response.status_code == 418:
            raise DependencyUnavailableError("Binance Testnet returned HTTP 418.")
        if response.status_code == 451:
            raise DependencyUnavailableError("Binance Testnet returned HTTP 451.")
        if response.status_code >= 500:
            raise ReconciliationRequiredError(
                "Binance Testnet returned a server error.",
                details={"http_status": response.status_code},
            )
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {}
            code = body.get("code") if isinstance(body, dict) else None
            if code in {-2010, -1007, -1001}:
                raise ReconciliationRequiredError(
                    "Binance Testnet rejected an order with an ambiguous code.",
                    details={"code": str(code), "http_status": response.status_code},
                )
            raise DependencyBadResponseError(
                "Binance Testnet rejected the request.",
                details={"http_status": response.status_code, "code": str(code)},
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ReconciliationRequiredError(
                "Binance Testnet returned non-JSON; reconcile before retrying."
            ) from exc
        if not isinstance(data, dict):
            raise ReconciliationRequiredError("Binance Testnet response was not an object.")
        return data

    async def submit(
        self,
        approved: ApprovedTradeIntent,
        market: MarketSnapshot,
        rules: SymbolFilters,
    ) -> ExecutionReceipt:
        self._require_configured()
        intent = approved.intent
        order_id = str(uuid4())
        params: dict[str, Any] = {
            "symbol": intent.symbol,
            "side": intent.side.value.upper(),
            "type": intent.order_preference.value,
            "quantity": canonical_str(approved.approved_qty),
            "newClientOrderId": client_order_id(intent.idempotency_key),
        }
        if intent.order_preference.value == "LIMIT":
            params["timeInForce"] = "GTC"
            params["price"] = canonical_str(approved.approved_limit_price or market.ask)
        data = await self._request("POST", "/api/v3/order", params)
        return await self._record_response(approved, order_id, data)

    async def _record_response(
        self, approved: ApprovedTradeIntent, order_id: str, data: dict[str, Any]
    ) -> ExecutionReceipt:
        raw_status = str(data.get("status", "")).upper()
        status = STATUS_MAP.get(raw_status)
        if status is None:
            raise ReconciliationRequiredError(
                "Binance Testnet returned an unrecognized order status.",
                details={"status": raw_status or "missing"},
            )
        fills: list[ExecutionFill] = []
        for item in data.get("fills") or []:
            try:
                qty = Decimal(str(item.get("qty", "0")))
                price = Decimal(str(item.get("price", "0")))
                commission = Decimal(str(item.get("commission", "0")))
            except Exception:  # noqa: BLE001
                raise ReconciliationRequiredError("Binance Testnet fill was malformed.")
            fee_quote = (
                commission
                if str(item.get("commissionAsset", "")).upper() == "USDT"
                else ZERO
            )
            fills.append(
                ExecutionFill(qty=qty, price=price, fee_quote=fee_quote, fee_asset="USDT")
            )
        executed_qty = Decimal(str(data.get("executedQty", "0")))
        cummulative_quote = Decimal(str(data.get("cummulativeQuoteQty", "0")))
        avg_price = (
            cummulative_quote / executed_qty if executed_qty > ZERO else ZERO
        )
        if not fills and executed_qty > ZERO and avg_price > ZERO:
            fills = [
                ExecutionFill(
                    qty=executed_qty,
                    price=avg_price,
                    fee_quote=ZERO,
                    fee_asset="USDT",
                )
            ]
        order, position = await self.repo.record_execution(
            approved=approved,
            order_id=order_id,
            exchange_order_id=str(data.get("orderId") or ""),
            status=status,
            fills=fills,
            account_id=str(approved.intent.account_id),
        )
        await self.repo.update_intent_status(str(approved.intent.intent_id), status.value)
        return ExecutionReceipt(
            order_id=order.id,
            exchange_order_id=order.exchange_order_id,
            status=status,
            filled_qty=executed_qty,
            avg_fill_price=avg_price,
            fee_quote=sum((fill.fee_quote for fill in fills), ZERO),
            position_id=position.id if position is not None else None,
            message=f"binance testnet status {raw_status}",
        )

    async def cancel(self, order_id: str) -> None:
        self._require_configured()
        order = await self.repo.get_order(order_id)
        if order is None or not order.exchange_order_id:
            raise ReconciliationRequiredError(
                "Cannot cancel an order without an exchange order id."
            )
        await self._request(
            "DELETE",
            "/api/v3/order",
            {"symbol": order.symbol, "orderId": order.exchange_order_id},
        )
        await self.repo.update_order_from_exchange(
            order_id, status=OrderStatus.CANCELLED.value, note="cancel acknowledged"
        )

    async def reconcile(self, account_id: str) -> ReconciliationResult:
        self._require_configured()
        orders = [
            order
            for order in await self.repo.list_orders(limit=200)
            if order.mode == "testnet"
            and order.status
            in {
                OrderStatus.ACCEPTED.value,
                OrderStatus.PARTIALLY_FILLED.value,
                OrderStatus.UNKNOWN.value,
            }
        ]
        checked = 0
        unknown = 0
        notes: list[str] = []
        for order in orders:
            if not order.exchange_order_id:
                unknown += 1
                notes.append(f"{order.id}: missing exchange order id")
                continue
            try:
                data = await self._request(
                    "GET",
                    "/api/v3/order",
                    {"symbol": order.symbol, "orderId": order.exchange_order_id},
                )
            except Exception as exc:  # noqa: BLE001
                unknown += 1
                notes.append(f"{order.id}: {type(exc).__name__}")
                continue
            raw_status = str(data.get("status", "")).upper()
            mapped = STATUS_MAP.get(raw_status)
            checked += 1
            if mapped is None:
                unknown += 1
                notes.append(f"{order.id}: unknown status {raw_status}")
                continue
            executed_qty = Decimal(str(data.get("executedQty", "0")))
            quote = Decimal(str(data.get("cummulativeQuoteQty", "0")))
            avg_price = quote / executed_qty if executed_qty > ZERO else None
            await self.repo.update_order_from_exchange(
                order.id,
                status=mapped.value,
                filled_qty=executed_qty,
                avg_fill_price=avg_price,
                note=f"reconciled from {raw_status}",
            )
        return ReconciliationResult(
            healthy=unknown == 0,
            checked_orders=checked,
            unknown_orders=unknown,
            notes=notes
            + [
                "Position/PnL rebuild from exchange fills remains a follow-up "
                "milestone; order state is reconciled."
            ],
        )

    async def health(self) -> dict[str, object]:
        try:
            response = await self._client.get(self.base_url + "/api/v3/time")
            return {
                "healthy": response.status_code == 200,
                "backend": self.name,
                "configured": self.configured,
                "http_status": response.status_code,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "healthy": False,
                "backend": self.name,
                "configured": self.configured,
                "error": type(exc).__name__,
            }

    async def close(self) -> None:
        await self._client.aclose()
