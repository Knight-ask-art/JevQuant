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

from ...domain.enums import IntentType, OrderStatus, SignalState
from ...domain.errors import (
    DependencyBadResponseError,
    DependencyUnavailableError,
    ProviderRateLimitedError,
    ReconciliationRequiredError,
)
from ...domain.intent import ApprovedTradeIntent
from ...domain.market import MarketSnapshot, SymbolFilters
from ...domain.money import ZERO, canonical_str
from ...services.balances import BalanceView
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


# A venue-reported price outside this band is treated as malformed rather than
# trusted. Every allowed symbol trades far inside it.
SANE_PRICE_MIN = Decimal("0.00000001")
SANE_PRICE_MAX = Decimal("10000000")


def rules_sane(price: Decimal) -> bool:
    return SANE_PRICE_MIN <= price <= SANE_PRICE_MAX


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
        """Bring working orders and the position ledger in line with the venue.

        Order state is always reconciled from ``GET /api/v3/order``. The
        position ledger is then rebuilt from the fills the venue confirms, so a
        close that filled while the local projection had it resting becomes a
        closed position with realized PnL and cash, not a permanently open one.
        """

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
            avg_price = None
            if executed_qty > ZERO and quote > ZERO:
                avg_price = quote / executed_qty
                if not rules_sane(avg_price):
                    unknown += 1
                    notes.append(f"{order.id}: venue returned an implausible average price")
                    continue
            elif executed_qty > ZERO:
                unknown += 1
                notes.append(f"{order.id}: filled quantity without a quote amount")
                continue
            try:
                fee_quote = await self._ingest_fills(order.id, data)
                # The account ledger must use the same weighted average the
                # order row shows, otherwise reconciliation PnL is inconsistent.
                fills = await self.repo.list_fills_for_order(order.id)
                total_qty = sum((fill.qty for fill in fills), ZERO)
                if total_qty > ZERO:
                    avg_price = (
                        sum((fill.qty * fill.price for fill in fills), ZERO) / total_qty
                    )
                if fills:
                    fee_quote = sum((fill.fee_quote for fill in fills), ZERO)
            except DependencyBadResponseError as exc:
                unknown += 1
                notes.append(f"{order.id}: {exc.message}")
                continue
            await self.repo.update_order_from_exchange(
                order.id,
                status=mapped.value,
                filled_qty=executed_qty,
                avg_fill_price=avg_price,
                fee_quote=fee_quote,
                note=f"reconciled from {raw_status}",
            )

        try:
            notes.extend(await self.reconcile_account(account_id))
        except Exception as exc:  # noqa: BLE001
            unknown += 1
            notes.append(f"account ledger: {type(exc).__name__}")
        return ReconciliationResult(
            healthy=unknown == 0,
            checked_orders=checked,
            unknown_orders=unknown,
            notes=notes,
        )

    async def _ingest_fills(self, order_id: str, data: dict[str, Any]) -> Decimal:
        """Persist the venue's individual trades exactly once; return their fees."""

        trades = data.get("fills")
        if not isinstance(trades, list):
            trades = []
        total_fee = ZERO
        for index, trade in enumerate(trades):
            if not isinstance(trade, dict):
                continue
            # Accept only the documented submit schema
            # {price, qty, commission, commissionAsset}. Anything else is
            # treated as unknown rather than silently counted as fill 0.
            if not {"price", "qty", "commission", "commissionAsset"} <= set(trade):
                raise DependencyBadResponseError(
                    "Binance Testnet fill list is not the expected schema."
                )
            commission = Decimal(str(trade.get("commission", "0")))
            fee_quote = (
                commission
                if str(trade.get("commissionAsset", "")).upper() == "USDT"
                else ZERO
            )
            trade_id = trade.get("id") or trade.get("tradeId") or f"{order_id}:{index}"
            await self.repo.add_exchange_fill(
                order_id=order_id,
                trade_id=str(trade_id),
                qty=Decimal(str(trade.get("qty", "0"))),
                price=Decimal(str(trade.get("price", "0"))),
                fee_quote=fee_quote,
            )
            total_fee += fee_quote
        return total_fee

    async def fetch_balances(self) -> BalanceView | None:
        """The venue account view, or ``None`` when this route is not configured.

        Only asset codes and meaningful amounts are kept; assets the venue
        reports as exactly zero are dropped so the view stays small.
        """

        if not self.configured:
            return None
        data = await self._request("GET", "/api/v3/account", {})
        balances = data.get("balances")
        if not isinstance(balances, list):
            raise DependencyBadResponseError(
                "Binance Testnet account response has no balances."
            )
        quote = "USDT"
        free_quote = ZERO
        locked_quote = ZERO
        base_quantity: dict[str, Decimal] = {}
        for item in balances:
            if not isinstance(item, dict):
                continue
            asset = str(item.get("asset", "")).upper()
            if not asset:
                continue
            try:
                free = Decimal(str(item.get("free", "0")))
                locked = Decimal(str(item.get("locked", "0")))
            except Exception:  # noqa: BLE001
                raise DependencyBadResponseError(
                    "Binance Testnet balance entry was malformed."
                )
            if asset == quote:
                free_quote += free
                locked_quote += locked
            elif free + locked > ZERO:
                base_quantity[asset] = free + locked
        # Without per-asset step sizes from exchangeInfo, compare base amounts
        # exactly: an unexplained base difference is always worth reporting.
        base_tolerance = {asset: ZERO for asset in base_quantity}
        return BalanceView(
            route="testnet",
            venue="binance_testnet",
            quote_asset=quote,
            free_quote=free_quote,
            locked_quote=locked_quote,
            base_quantity=base_quantity,
            base_tolerance=base_tolerance,
            is_simulated=self.is_simulated,
            raw_summary={"account_type": str(data.get("accountType", ""))},
        )

    async def reconcile_account(self, account_id: str) -> list[str]:
        """Rebuild position, cash and realized PnL from venue-confirmed closes."""

        account = await self.repo.get_account(account_id)
        if account is None:
            raise ValueError(f"account {account_id} not found")
        notes: list[str] = []
        delta_cash = ZERO
        delta_realized = ZERO
        adopted = 0
        for position in await self.repo.get_open_positions(account_id):
            if position.mode != "testnet":
                continue
            signal = await self.repo.get_signal(position.signal_id)
            if signal is None or signal.state != SignalState.EXIT_PENDING.value:
                continue
            close_intent = await self.repo.get_signal_intent(
                signal.id, intent_type=IntentType.CLOSE
            )
            if close_intent is None:
                continue
            exchange_order = await self.repo.get_order_for_intent(close_intent.id)
            if exchange_order is None or exchange_order.status != OrderStatus.FILLED.value:
                continue

            exit_qty = exchange_order.filled_qty or ZERO
            exit_price = exchange_order.avg_fill_price or ZERO
            if exit_qty <= ZERO or exit_price <= ZERO or position.qty <= ZERO:
                notes.append(f"{position.symbol}: filled close missing quantity or price")
                continue
            qty = min(exit_qty, position.qty)
            exit_fee = exchange_order.fee_quote or ZERO
            entry_fee = await self._entry_fee(position)
            entry_fee_alloc = entry_fee * qty / position.qty
            realized = qty * (exit_price - position.avg_entry_price) - exit_fee - entry_fee_alloc

            delta_cash += qty * exit_price - exit_fee
            delta_realized += realized
            remaining = position.qty - qty
            closed = remaining <= position.qty * Decimal("0.00000001")
            await self.repo.settle_reconciled_close(
                position.id,
                qty=remaining,
                realized_pnl_usdt=(position.realized_pnl_usdt or ZERO) + realized,
                exit_order_id=exchange_order.id,
                closed=closed,
            )
            await self.repo.transition_signal(
                signal.id,
                SignalState.CLOSED,
                {"reason": "exchange_close_confirmed", "order_id": exchange_order.id},
            )
            await self.repo.update_intent_status(close_intent.id, "EXECUTED")
            adopted += 1
            notes.append(f"{position.symbol}: exchange close confirmed; position reconciled")

        if delta_cash != ZERO or delta_realized != ZERO:
            await self.repo.adjust_account_cash(
                account_id, cash_delta=delta_cash, realized_delta=delta_realized
            )
        if adopted:
            notes.append(f"{adopted} position(s) rebuilt from exchange fills")
        return notes

    async def _entry_fee(self, position) -> Decimal:
        if not position.entry_order_id:
            return ZERO
        entry = await self.repo.get_order(position.entry_order_id)
        if entry is None:
            return ZERO
        if entry.fee_quote and entry.fee_quote > ZERO:
            return entry.fee_quote
        return sum(
            (fill.fee_quote for fill in await self.repo.list_fills_for_order(entry.id)),
            ZERO,
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
