"""Periodic exchange-account balance sync.

The ledger and the venue can drift for benign reasons (dust, rounding) and for
urgent ones (a fill the ledger never saw). The sync keeps a time series of the
venue's view and raises an audit event when the drift leaves tolerance. It never
edits the ledger: money moves only through order receipts or reconciliation.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ..domain.enums import Mode
from ..domain.money import canonical_str
from ..storage.repository import Repository
from .audit import AuditService
from .balances import (
    BalanceDriftReport,
    BalanceView,
    balance_row,
    compare,
    drift_payload,
    ledger_base_quantities,
)
from .events import EventBroker

logger = logging.getLogger("jev_trading.balances")

# Routes whose balances are worth comparing with the venue. Paper and shadow
# are synthetic by construction and have no venue account.
SYNCED_ROUTES = (Mode.TESTNET, Mode.LIVE)


class BalanceSyncService:
    def __init__(
        self,
        *,
        repo: Repository,
        audit: AuditService,
        broker: EventBroker,
        adapters: dict[Mode, Any],
        account_id: str,
        base_assets: dict[str, str],
    ) -> None:
        self.repo = repo
        self.audit = audit
        self.broker = broker
        self.adapters = adapters
        self.account_id = account_id
        self.base_assets = base_assets

    async def sync(self) -> BalanceDriftReport:
        """Read every synced route's venue account and compare it to the ledger."""

        account = await self.repo.get_account(self.account_id)
        ledger_quote = account.cash_quote_usdt if account is not None else Decimal("0")
        positions = await self.repo.get_open_positions(self.account_id)
        now = datetime.now(UTC)
        drifts = []
        skipped: dict[str, str] = {}
        for route in SYNCED_ROUTES:
            adapter = self.adapters.get(route)
            if adapter is None:
                skipped[route.value] = "no adapter for route"
                continue
            fetch = getattr(adapter, "fetch_balances", None)
            if not callable(fetch):
                skipped[route.value] = "adapter exposes no venue balance"
                continue
            try:
                view: BalanceView | None = await fetch()
            except Exception as exc:  # noqa: BLE001
                # Secrets never reach the message: only the error type.
                skipped[route.value] = f"balance read failed ({type(exc).__name__})"
                await self.audit.record(
                    "balance.sync_failed",
                    "system",
                    route.value,
                    {"route": route.value, "error": type(exc).__name__},
                )
                continue
            if view is None:
                skipped[route.value] = "route not configured"
                continue

            route_positions = [p for p in positions if p.mode == route.value]
            ledger_base = ledger_base_quantities(route_positions, self.base_assets)
            drift = compare(view, ledger_quote=ledger_quote, ledger_base=ledger_base)
            await self.repo.insert_balance_snapshot(
                balance_row(view, ledger_quote, created_at=now)
            )
            drifts.append(drift)
            await self.audit.record(
                "balance.synced", "system", route.value, drift_payload(drift)
            )
            if not drift.within_tolerance:
                await self._raise_alarm(drift)
        report = BalanceDriftReport(drifts=drifts, skipped=skipped)
        await self.broker.publish(
            "system.events",
            {
                "event": "balance.sync",
                "routes": [drift.route for drift in drifts],
                "skipped": skipped,
            },
        )
        return report

    async def _raise_alarm(self, drift) -> None:
        payload = drift_payload(drift)
        logger.warning(
            "balance drift route=%s difference=%s",
            drift.route,
            canonical_str(drift.difference),
        )
        await self.audit.record("balance.drift", "system", drift.route, payload)
        await self.broker.publish("system.events", {"event": "balance.drift", **payload})


class BalanceSyncScheduler:
    """Runs :meth:`BalanceSyncService.sync` on a wall-clock interval.

    A failed run is audited and the loop continues: a venue outage must not
    stop the ledger, and the next run picks it up.
    """

    def __init__(self, service: BalanceSyncService, *, interval_seconds: int) -> None:
        self.service = service
        self.interval_seconds = max(1, interval_seconds)

    async def loop(self) -> None:
        import asyncio

        while True:
            try:
                await asyncio.sleep(self.interval_seconds)
                report = await self.service.sync()
                logger.info(
                    "balance sync routes=%s skipped=%s",
                    [d.route for d in report.drifts],
                    report.skipped,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("balance sync failed")
                try:
                    await self.service.audit.record(
                        "balance.sync_failed",
                        "system",
                        None,
                        {"error": type(exc).__name__},
                    )
                except Exception:  # noqa: BLE001
                    pass
