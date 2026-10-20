"""Execution routes and their exchange-account balance sync.

Each route that owns real (or testnet) balances can expose the venue's account
view. The sync service compares it with the ledger that the adapters already
maintains and reports — it never invents a fill from a balance difference.

What it does record:

* the venue's view in ``balance_snapshots`` so drift becomes a time series;
* a redacted audit event whenever the drift leaves the tolerance;
* a ``balance.sync_failed`` audit event when the venue cannot be read.

It does **not** move cash or close positions. Settling differences requires
interpreting venue trades, which is reconciliation's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from ..domain.money import canonical_str
from ..storage.models import BalanceSnapshotRow
from ..storage.repository import PositionRow

# Absolute tolerance for the quote balance. Anything smaller is exchange-side
# rounding and dust; anything larger deserves an operator's eyes.
QUOTE_TOLERANCE_USDT = Decimal("0.01")


def _amount_quantum(value: Decimal) -> Decimal:
    """One decimal finer than the value's own resolution, floored at 1e-12."""

    exponent = value.normalize().as_tuple().exponent
    return Decimal(1).scaleb(max(exponent - 1, -12))


@dataclass(frozen=True)
class BalanceView:
    """A read-only view of one venue account on one route."""

    route: str
    venue: str
    quote_asset: str
    free_quote: Decimal
    locked_quote: Decimal
    base_quantity: dict[str, Decimal]
    # Per-asset quantity tolerance (the venue's LOT_SIZE step). A base
    # difference within it is dust.
    base_tolerance: dict[str, Decimal]
    is_simulated: bool
    raw_summary: dict[str, Any]

    @property
    def total_quote(self) -> Decimal:
        return self.free_quote + self.locked_quote

    def to_payload(self) -> dict[str, Any]:
        """JSON-safe, secret-free representation (asset codes and amounts only)."""

        return {
            "venue": self.venue,
            "quote_asset": self.quote_asset,
            "free_quote": canonical_str(self.free_quote),
            "locked_quote": canonical_str(self.locked_quote),
            "total_quote": canonical_str(self.total_quote),
            "is_simulated": self.is_simulated,
            **self.raw_summary,
        }


@dataclass(frozen=True)
class BalanceDrift:
    route: str
    quote_asset: str
    ledger_quote: Decimal
    venue_quote: Decimal
    difference: Decimal
    within_tolerance: bool
    base_mismatch: dict[str, dict[str, str]]
    checked_at: datetime


@dataclass(frozen=True)
class BalanceDriftReport:
    drifts: list[BalanceDrift]
    skipped: dict[str, str]  # route -> reason no venue view was read
    # route -> outcome of the reconciliation a drift triggered (empty when
    # every route agreed, or when auto-reconciliation is off).
    reconciled: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def in_tolerance(self) -> bool:
        return all(drift.within_tolerance for drift in self.drifts)


def ledger_base_quantities(
    positions: list[PositionRow], base_assets: dict[str, str]
) -> dict[str, Decimal]:
    """Base-asset amounts the ledger believes are held (open positions only)."""

    balances: dict[str, Decimal] = {}
    for position in positions:
        asset = base_assets.get(position.symbol)
        if asset is None:
            continue
        balances[asset] = balances.get(asset, Decimal("0")) + position.qty
    return balances


def compare(
    view: BalanceView,
    *,
    ledger_quote: Decimal,
    ledger_base: dict[str, Decimal],
    tracked_assets: set[str] | None = None,
) -> BalanceDrift:
    """Compare one venue view with the ledger projection at the same instant.

    ``tracked_assets`` limits base comparison to the assets the ledger can
    actually hold (the allowed symbol universe). Faucet or airdrop tokens the
    bot never trades are not ledger discrepancies and must not flood the drift
    report; the venue view itself is still recorded.
    """

    quantum = _amount_quantum(view.total_quote + ledger_quote)
    difference = (view.total_quote - ledger_quote).quantize(quantum)
    base_mismatch: dict[str, dict[str, str]] = {}
    assets = set(view.base_quantity) | set(ledger_base)
    if tracked_assets is not None:
        assets &= tracked_assets
    for asset in sorted(assets):
        venue_qty = view.base_quantity.get(asset, Decimal("0"))
        ledger_qty = ledger_base.get(asset, Decimal("0"))
        step = view.base_tolerance.get(asset, Decimal("0"))
        if abs(venue_qty - ledger_qty) <= step:
            continue
        base_mismatch[asset] = {
            "ledger": canonical_str(ledger_qty),
            "venue": canonical_str(venue_qty),
            "difference": canonical_str(venue_qty - ledger_qty),
        }
    quote_ok = abs(view.total_quote - ledger_quote) <= QUOTE_TOLERANCE_USDT
    return BalanceDrift(
        route=view.route,
        quote_asset=view.quote_asset,
        ledger_quote=ledger_quote,
        venue_quote=view.total_quote,
        difference=difference,
        within_tolerance=quote_ok and not base_mismatch,
        base_mismatch=base_mismatch,
        checked_at=datetime.now(UTC),
    )


def balance_row(
    view: BalanceView, ledger_quote: Decimal, *, created_at: datetime
) -> BalanceSnapshotRow:
    """Persist a venue view plus the ledger's figure at that same moment."""

    return BalanceSnapshotRow(
        id=str(uuid4()),
        source=view.route,
        asset=view.quote_asset,
        free=view.free_quote,
        locked=view.locked_quote,
        total=view.total_quote,
        is_simulated=view.is_simulated,
        raw_json={**view.to_payload(), "ledger_quote": canonical_str(ledger_quote)},
        created_at=created_at,
    )


def drift_payload(drift: BalanceDrift) -> dict[str, Any]:
    return {
        "route": drift.route,
        "quote_asset": drift.quote_asset,
        "ledger_quote": canonical_str(drift.ledger_quote),
        "venue_quote": canonical_str(drift.venue_quote),
        "difference": canonical_str(drift.difference),
        "within_tolerance": drift.within_tolerance,
        "base_mismatch": drift.base_mismatch,
    }
