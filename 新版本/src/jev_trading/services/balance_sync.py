"""Periodic exchange-account balance sync.

The ledger and the venue can drift for benign reasons (dust, rounding) and for
urgent ones (a fill the ledger never saw). The sync keeps a time series of the
venue's view and raises an audit event when the drift leaves tolerance. It never
edits the ledger: money moves only through order receipts or reconciliation.

A drift also triggers **one** reconciliation for that route (the adapter's
``reconcile``), rate-limited per route, because the usual cause is a fill the
ledger never saw. The sync itself still never moves cash or closes positions:
only reconciliation may settle a difference, from venue-confirmed trades.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

from ..domain.enums import Mode
from ..domain.money import canonical_str
from ..storage.repository import Repository
from .audit import AuditService
from .balances import (
    BalanceDrift,
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
        auto_reconcile: bool = True,
        reconcile_cooldown_seconds: int = 300,
        clock: Callable[[], datetime] | None = None,
        reconcile_lock: asyncio.Lock | None = None,
    ) -> None:
        self.repo = repo
        self.audit = audit
        self.broker = broker
        self.adapters = adapters
        self.account_id = account_id
        self.base_assets = base_assets
        self.auto_reconcile = auto_reconcile
        self.reconcile_cooldown_seconds = max(0, reconcile_cooldown_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))
        # Per-route timestamp of the last auto-reconciliation, for the cooldown.
        self._last_reconcile: dict[str, datetime] = {}
        # Manual and scheduled syncs share the snapshots and the reconcile path;
        # one at a time keeps a drift from being reconciled twice concurrently.
        self._sync_lock = asyncio.Lock()
        # Shared with the operator reconcile endpoint so the two paths cannot
        # settle the same confirmed close twice.
        self._reconcile_lock = reconcile_lock or asyncio.Lock()

    async def sync(self) -> BalanceDriftReport:
        """Read every synced route's venue account and compare it to the ledger.

        Serialized: the API and the scheduler may call this at the same time.
        """

        async with self._sync_lock:
            return await self._sync()

    async def _sync(self) -> BalanceDriftReport:
        account = await self.repo.get_account(self.account_id)
        ledger_quote = account.cash_quote_usdt if account is not None else Decimal("0")
        positions = await self.repo.get_open_positions(self.account_id)
        now = self._clock()
        drifts = []
        skipped: dict[str, str] = {}
        reconciled: dict[str, dict[str, Any]] = {}
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
                outcome = await self._reconcile_after_drift(route, adapter, drift)
                if outcome is not None:
                    reconciled[route.value] = outcome
        report = BalanceDriftReport(
            drifts=drifts, skipped=skipped, reconciled=reconciled
        )
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

    async def _reconcile_after_drift(
        self, route: Mode, adapter: Any, drift: BalanceDrift
    ) -> dict[str, Any] | None:
        """Run one reconciliation for a route whose balance left tolerance.

        Reconciliation is the only path that may settle a drift: it ingests
        venue-confirmed fills and rebuilds the position ledger. It is rate
        limited per route so a persistent difference cannot turn every sync
        into a burst of venue calls, and a failed attempt still starts the
        cooldown rather than hammering an unhealthy venue.
        """

        if not self.auto_reconcile:
            return None
        reconcile = getattr(adapter, "reconcile", None)
        if not callable(reconcile):
            return {"triggered": False, "reason": "adapter exposes no reconcile"}

        now = self._clock()
        last = self._last_reconcile.get(route.value)
        if (
            last is not None
            and self.reconcile_cooldown_seconds > 0
            and now - last < timedelta(seconds=self.reconcile_cooldown_seconds)
        ):
            return {
                "triggered": False,
                "reason": "cooldown",
                "last_reconcile_at": last.isoformat(),
                "cooldown_seconds": self.reconcile_cooldown_seconds,
            }

        payload = drift_payload(drift)
        await self.audit.record(
            "balance.reconcile_triggered", "system", route.value, payload
        )
        # Start the cooldown before the call: a failure must not retry in a loop.
        self._last_reconcile[route.value] = now
        try:
            async with self._reconcile_lock:
                result = await reconcile(self.account_id)
        except Exception as exc:  # noqa: BLE001
            # Secrets never reach the message: only the error type.
            await self.audit.record(
                "balance.reconcile_failed",
                "system",
                route.value,
                {"route": route.value, "error": type(exc).__name__},
            )
            return {
                "triggered": True,
                "healthy": False,
                "error": type(exc).__name__,
                "difference": payload["difference"],
            }

        outcome: dict[str, Any] = {
            "triggered": True,
            "healthy": bool(getattr(result, "healthy", False)),
            "checked_orders": int(getattr(result, "checked_orders", 0)),
            "unknown_orders": int(getattr(result, "unknown_orders", 0)),
            "notes": list(getattr(result, "notes", []))[:20],
            "difference": payload["difference"],
        }
        account = await self.repo.get_account(self.account_id)
        if account is not None:
            # Reconciliation may have adjusted cash from venue-confirmed closes.
            outcome["ledger_quote_after"] = canonical_str(account.cash_quote_usdt)
        await self.audit.record(
            "balance.reconcile_finished", "system", route.value, outcome
        )
        await self.broker.publish(
            "system.events",
            {"event": "balance.reconciled", "route": route.value, **outcome},
        )
        return outcome


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
