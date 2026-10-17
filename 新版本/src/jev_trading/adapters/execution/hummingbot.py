"""Hummingbot V2 execution adapter.

Milestone note: this adapter maps an approved intent to a PositionExecutor
payload and sends it to the Hummingbot API.  It has NOT been validated
against a live Hummingbot instance in this repository.  Any ambiguous
response becomes RECONCILIATION_REQUIRED; it is never blindly retried.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx

from ...domain.enums import OrderStatus
from ...domain.errors import (
    DependencyBadResponseError,
    DependencyUnavailableError,
    ReconciliationRequiredError,
)
from ...domain.intent import ApprovedTradeIntent
from ...domain.market import MarketSnapshot, SymbolFilters
from ...services.balances import BalanceView
from ...storage.repository import Repository
from .base import ExecutionReceipt, ReconciliationResult


def normalize_pair(symbol: str) -> str:
    if "-" in symbol:
        return symbol
    if symbol.endswith("USDT"):
        return f"{symbol[:-4]}-USDT"
    return symbol


def bps_to_decimal(bps) -> str:
    return str((bps or 0) / 10000)


class HummingbotExecutionAdapter:
    name = "hummingbot"

    def __init__(
        self,
        repo: Repository,
        *,
        base_url: str | None,
        api_key: str | None,
        connector_name: str = "binance",
        timeout: float = 20.0,
    ) -> None:
        self.repo = repo
        self.base_url = (base_url or "").rstrip("/")
        self._api_key = api_key or ""
        self.connector_name = connector_name
        self._client = httpx.AsyncClient(timeout=timeout)
        self.configured = bool(self.base_url and self._api_key)

    def _require_configured(self) -> None:
        if not self.configured:
            raise DependencyUnavailableError("Hummingbot is not configured.")

    def to_position_executor(self, approved: ApprovedTradeIntent) -> dict[str, Any]:
        intent = approved.intent
        return {
            "connector_name": self.connector_name,
            "trading_pair": normalize_pair(intent.symbol),
            "side": intent.side.value.upper(),
            "amount_quote": str(approved.effective_quote_notional),
            "triple_barrier_config": {
                "stop_loss": bps_to_decimal(approved.effective_stop_loss_bps),
                "take_profit": bps_to_decimal(approved.effective_take_profit_bps),
                "time_limit": approved.effective_max_holding_seconds,
            },
            "metadata": {
                "intent_id": str(intent.intent_id),
                "policy_id": str(intent.policy_id),
                "signal_id": str(intent.signal_id),
            },
        }

    async def submit(
        self,
        approved: ApprovedTradeIntent,
        market: MarketSnapshot,
        rules: SymbolFilters,
    ) -> ExecutionReceipt:
        self._require_configured()
        payload = self.to_position_executor(approved)
        order_id = str(uuid4())
        try:
            response = await self._client.post(
                self.base_url + "/executors",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise ReconciliationRequiredError(
                "Hummingbot submission result is unknown; reconcile before retrying.",
                details={"reason": type(exc).__name__},
            ) from exc
        if response.status_code == 429:
            raise DependencyUnavailableError("Hummingbot is rate limited.")
        if response.status_code >= 500:
            raise ReconciliationRequiredError(
                "Hummingbot returned a server error; submission outcome is unknown.",
                details={"http_status": response.status_code},
            )
        if response.status_code >= 400:
            raise DependencyBadResponseError(
                "Hummingbot rejected the executor.",
                details={"http_status": response.status_code},
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ReconciliationRequiredError(
                "Hummingbot returned non-JSON; submission outcome is unknown."
            ) from exc
        if not isinstance(data, dict):
            raise ReconciliationRequiredError("Hummingbot response was not an object.")
        status = str(data.get("status", "")).upper()
        executor_id = data.get("executor_id") or data.get("id")
        if not executor_id or status not in {
            "PENDING",
            "ACTIVE",
            "OPEN",
            "CREATED",
            "FILLED",
            "COMPLETED",
        }:
            raise ReconciliationRequiredError(
                "Hummingbot returned an unrecognized executor state.",
                details={"status": status or "missing"},
            )
        mapped = (
            OrderStatus.FILLED if status in {"FILLED", "COMPLETED"} else OrderStatus.ACCEPTED
        )
        await self.repo.record_execution(
            approved=approved,
            order_id=order_id,
            exchange_order_id=str(executor_id),
            status=mapped,
            fills=[],
            account_id=str(approved.intent.account_id),
        )
        await self.repo.update_intent_status(str(approved.intent.intent_id), mapped.value)
        return ExecutionReceipt(
            order_id=order_id,
            exchange_order_id=str(executor_id),
            status=mapped,
            reconciliation_required=True,
            message="Hummingbot accepted the executor; fills await reconciliation.",
        )

    async def cancel(self, order_id: str) -> None:
        self._require_configured()
        try:
            await self._client.delete(
                f"{self.base_url}/executors/{order_id}",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        except httpx.HTTPError as exc:
            raise ReconciliationRequiredError(
                "Hummingbot cancel outcome is unknown."
            ) from exc

    async def fetch_balances(self) -> BalanceView | None:
        """Connector balances from Hummingbot, mapped onto a route-agnostic view.

        Hummingbot reports ``[base_available, base_total, quote_available,
        quote_total]`` per trading pair. Base totals are summed per asset and
        the quote totals per quote asset. The response shape is *not* verified
        against a running Hummingbot, so an unexpected payload raises instead of
        being guessed at.
        """

        if not self.configured:
            return None
        pairs = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
        try:
            response = await self._client.post(
                self.base_url + "/accounts/balances",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"account_name": self.connector_name, "trading_pairs": pairs},
            )
        except httpx.HTTPError as exc:
            raise DependencyUnavailableError(
                "Hummingbot balance request failed.", details={"reason": type(exc).__name__}
            ) from exc
        if response.status_code >= 400:
            raise DependencyBadResponseError(
                "Hummingbot rejected the balance request.",
                details={"http_status": response.status_code},
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise DependencyBadResponseError("Hummingbot returned non-JSON balances.") from exc
        if not isinstance(data, dict):
            raise DependencyBadResponseError("Hummingbot balance payload was not an object.")
        if "balances" not in data or not isinstance(data["balances"], dict):
            raise DependencyBadResponseError(
                "Hummingbot balance payload has no balances object."
            )
        # Never fall back to the whole payload: another connector's balances
        # shown as ours would be worse than reporting nothing.
        views = data["balances"].get(self.connector_name)
        if not isinstance(views, dict):
            raise DependencyBadResponseError(
                "Hummingbot balance payload did not contain the connector."
            )
        base_qty: dict[str, Decimal] = {}
        base_tol: dict[str, Decimal] = {}
        quote_free = Decimal("0")
        quote_locked = Decimal("0")
        for pair, entry in views.items():
            if not isinstance(entry, dict):
                raise DependencyBadResponseError(
                    "Hummingbot pair balance was not an object."
                )
            base, quote = (str(pair).split("-") + ["", ""])[:2]
            try:
                base_total = Decimal(str(entry.get("base_total", "0")))
                quote_free += Decimal(str(entry.get("quote_available", "0")))
                quote_locked_total = entry.get("quote_total")
                if quote_locked_total is not None:
                    quote_locked += Decimal(str(quote_locked_total)) - Decimal(
                        str(entry.get("quote_available", "0"))
                    )
            except Exception:  # noqa: BLE001
                raise DependencyBadResponseError(
                    "Hummingbot balance amounts were malformed."
                )
            if base and base_total > 0:
                base_qty[base] = base_qty.get(base, Decimal("0")) + base_total
                base_tol[base] = Decimal("0.00000001")
        return BalanceView(
            route="live",
            venue=self.connector_name,
            quote_asset="USDT",
            free_quote=quote_free,
            locked_quote=max(quote_locked, Decimal("0")),
            base_quantity=base_qty,
            base_tolerance=base_tol,
            is_simulated=False,
            raw_summary={"pairs": sorted(views)},
        )

    async def reconcile(self, account_id: str) -> ReconciliationResult:
        self._require_configured()
        try:
            response = await self._client.get(
                self.base_url + "/executors",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        except httpx.HTTPError as exc:
            return ReconciliationResult(
                healthy=False, notes=[f"Hummingbot unreachable: {type(exc).__name__}"]
            )
        if response.status_code >= 400:
            return ReconciliationResult(
                healthy=False, notes=[f"Hummingbot HTTP {response.status_code}"]
            )
        return ReconciliationResult(
            healthy=True,
            notes=[
                "Executor list fetched. Ledger updates from Hummingbot fills are a "
                "follow-up milestone; do not treat this as settled reconciliation."
            ],
        )

    async def health(self) -> dict[str, object]:
        if not self.configured:
            return {"healthy": False, "backend": self.name, "configured": False}
        try:
            response = await self._client.get(
                self.base_url + "/health",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            return {
                "healthy": response.status_code < 500,
                "backend": self.name,
                "http_status": response.status_code,
            }
        except Exception as exc:  # noqa: BLE001
            return {"healthy": False, "backend": self.name, "error": type(exc).__name__}

    async def close(self) -> None:
        await self._client.aclose()
