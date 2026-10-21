"""What the venue actually charged, next to what the model assumed.

The cost model is an assumption: a fee rate from the settings and two
slippage numbers that were never measured against a fill. That is fine as
long as it is conservative and visible -- it stops being fine when nobody can
tell whether it still describes the venue.

This report closes the loop with the only evidence that exists: the fills.
For each one it prices what was paid (``fee_quote`` over the traded notional)
and how far the fill landed from the snapshot the decision used, then puts
those medians next to the model's numbers.

Two things it deliberately does not do:

- **It does not change the model.** Correcting the assumption is a decision
  made with the number in hand, not a side effect of reading a report.
- **It does not count paper fills as evidence.** A paper fill's commission is
  the model's own arithmetic echoed back; including it would make the model
  look calibrated by construction.

Read-only. Never places, sizes or approves anything.

    python -m jev_trading.cost_report [--days 30] [--limit 500] [--json]
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Any

from ..domain.costs import BPS, CostModel

ZERO = Decimal("0")

#: Below this many venue fills a median describes the sample, not the venue.
MIN_FILLS_FOR_EVIDENCE = 10
#: A relative fee gap this large is worth a look; smaller is rounding noise.
FEE_TOLERANCE = Decimal("0.10")
#: Slippage is noisy fill to fill, so only a large absolute gap is reported.
SLIPPAGE_TOLERANCE_BPS = Decimal("5")
#: Fills from these modes are the model talking to itself, not the venue.
MODELLED_ONLY_MODES = frozenset({"paper"})


def _median(values: Iterable[Decimal | None]) -> float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return round(float(median(present)), 4)


def _mean(values: Iterable[Decimal | None]) -> float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return round(float(sum(present, ZERO) / len(present)), 4)


@dataclass(frozen=True)
class CostReport:
    """Realised fees and slippage over one set of fills."""

    model: CostModel
    fills: Sequence[Any]

    # -- decomposition ---------------------------------------------------
    def _traded(self) -> list[Any]:
        return [fill for fill in self.fills if fill.qty > ZERO and fill.price > ZERO]

    @staticmethod
    def fee_bps(fill: Any) -> Decimal | None:
        """The commission paid on this fill, as bps of its notional.

        ``None`` when the fee was charged in an asset the system cannot price
        (BNB). That is a missing measurement, not a zero.
        """

        if fill.qty <= ZERO or fill.price <= ZERO:
            return None
        if fill.fee_quote <= ZERO and fill.fee_amount > ZERO:
            return None
        return fill.fee_quote / (fill.qty * fill.price) * BPS

    @staticmethod
    def slippage_bps(fill: Any) -> Decimal | None:
        """How far the fill landed from the quote the decision was made on.

        Positive is worse: a buy filled above the ask, a sell below the bid.
        The reference is the *snapshot at decision time*, so a favourable later
        market is not credited to execution.
        """

        reference = fill.reference_price
        if reference is None or reference <= ZERO:
            return None
        if str(fill.side).upper() == "BUY":
            return (fill.price / reference - Decimal("1")) * BPS
        return (Decimal("1") - fill.price / reference) * BPS

    def _venue(self) -> list[Any]:
        return [fill for fill in self._traded() if fill.mode not in MODELLED_ONLY_MODES]

    # -- reports ---------------------------------------------------------
    def fee_report(self) -> dict[str, Any]:
        venue = self._venue()
        modelled = [self.fee_bps(fill) for fill in venue]
        unpriced = [
            fill for fill in venue if fill.fee_quote <= ZERO and fill.fee_amount > ZERO
        ]
        by_asset: dict[str, int] = {}
        for fill in venue:
            by_asset[fill.fee_asset] = by_asset.get(fill.fee_asset, 0) + 1
        return {
            "fills": len(venue),
            "priced_fills": sum(1 for value in modelled if value is not None),
            "unpriced_fills": len(unpriced),
            "unpriced_assets": sorted({fill.fee_asset for fill in unpriced}),
            "fee_assets": by_asset,
            "median_fee_bps": _median(modelled),
            "mean_fee_bps": _mean(modelled),
            "modelled_fee_bps": float(self.model.fee_bps),
        }

    def slippage_report(self) -> dict[str, Any]:
        venue = self._venue()
        entries = [fill for fill in venue if str(fill.intent_type).upper() == "OPEN"]
        exits = [fill for fill in venue if str(fill.intent_type).upper() == "CLOSE"]
        entry_slips = [self.slippage_bps(fill) for fill in entries]
        exit_slips = [self.slippage_bps(fill) for fill in exits]
        return {
            "entry_fills": len(entries),
            "median_entry_slippage_bps": _median(entry_slips),
            "modelled_entry_slippage_bps": float(self.model.entry_slippage_bps),
            "exit_fills": len(exits),
            "median_exit_slippage_bps": _median(exit_slips),
            "modelled_exit_slippage_bps": float(self.model.exit_slippage_bps),
        }

    def round_trip_report(self) -> dict[str, Any]:
        """Realised fee and slippage on both sides, the cost the gate compares to."""

        venue = self._venue()
        fees = [self.fee_bps(fill) for fill in venue]
        slips = [self.slippage_bps(fill) for fill in venue]
        fee_median = _median(fees)
        slip_median = _median(slips)
        realised = None
        if fee_median is not None:
            realised = fee_median * 2 + (slip_median or 0.0) * 2
        return {
            "realised_round_trip_bps": round(realised, 4) if realised is not None else None,
            "modelled_round_trip_bps": float(self.model.round_trip_bps),
        }

    def hypotheses(self) -> list[dict[str, str]]:
        """Named readings, each with the experiment it would justify."""

        fee = self.fee_report()
        slippage = self.slippage_report()
        out: list[dict[str, str]] = []

        # A fee that cannot be priced is a fact about every fill, not a
        # statistical claim, so it is reported whatever the sample size.
        if fee["unpriced_fills"]:
            out.append(
                {
                    "hypothesis": "fees_paid_in_an_unpriced_asset",
                    "reading": (
                        f"{fee['unpriced_fills']} fills charged in "
                        f"{', '.join(fee['unpriced_assets'])}; their cost is not in "
                        "any net figure"
                    ),
                    "would_test": "hold BNB to pay fees (or disable it) so the cost is visible",
                }
            )

        if fee["priced_fills"] < MIN_FILLS_FOR_EVIDENCE:
            out.append(
                {
                    "hypothesis": "not_enough_fills",
                    "reading": (
                        f"{fee['priced_fills']} priced venue fills; "
                        f"{MIN_FILLS_FOR_EVIDENCE} is the minimum to read a median"
                    ),
                    "would_test": "keep recording; do not change the model yet",
                }
            )
            return out

        realised = Decimal(str(fee["median_fee_bps"]))
        modelled = self.model.fee_bps
        if modelled > ZERO and realised > modelled * (Decimal("1") + FEE_TOLERANCE):
            out.append(
                {
                    "hypothesis": "fees_higher_than_modelled",
                    "reading": (
                        f"median realised fee {realised} bps vs modelled {modelled} bps"
                    ),
                    "would_test": "raise JEV_FEE_BPS; every net figure improves again",
                }
            )
        elif modelled > ZERO and realised < modelled * (Decimal("1") - FEE_TOLERANCE):
            out.append(
                {
                    "hypothesis": "fees_lower_than_modelled",
                    "reading": (
                        f"median realised fee {realised} bps vs modelled {modelled} bps"
                    ),
                    "would_test": "check the fee tier / BNB discount before lowering it",
                }
            )

        entry = slippage["median_entry_slippage_bps"]
        if entry is not None and Decimal(str(entry)) > (
            self.model.entry_slippage_bps + SLIPPAGE_TOLERANCE_BPS
        ):
            out.append(
                {
                    "hypothesis": "entry_slippage_underestimated",
                    "reading": (
                        f"median entry fill {entry} bps above the decision quote vs "
                        f"{self.model.entry_slippage_bps} bps modelled"
                    ),
                    "would_test": "replay with the measured number before resizing",
                }
            )

        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "fills": len(self.fills),
            "model": self.model.as_dict(),
            "fees": self.fee_report(),
            "slippage": self.slippage_report(),
            "round_trip": self.round_trip_report(),
            "hypotheses": self.hypotheses(),
        }


async def load(
    repo: Any, *, days: int = 30, limit: int = 500, model: CostModel | None = None
) -> CostReport:
    since = datetime.now(UTC) - timedelta(days=max(1, days))
    fills = await repo.list_realised_fills(since=since, limit=max(1, limit))
    return CostReport(model=model or CostModel(), fills=fills)


def format_report(report: dict[str, Any]) -> str:
    fees = report["fees"]
    slippage = report["slippage"]
    round_trip = report["round_trip"]
    assets = fees["fee_assets"] or "none"
    lines = [
        f"fills: {report['fills']}  venue fills priced: {fees['priced_fills']}  "
        f"unpriced: {fees['unpriced_fills']}",
        f"fee assets charged: {assets}",
        "",
        f"Fee per side: realised median {fees['median_fee_bps']} bps "
        f"(mean {fees['mean_fee_bps']}) vs modelled {fees['modelled_fee_bps']} bps",
        f"Entry slippage: realised median {slippage['median_entry_slippage_bps']} bps over "
        f"{slippage['entry_fills']} fills vs modelled "
        f"{slippage['modelled_entry_slippage_bps']} bps",
        f"Exit slippage: realised median {slippage['median_exit_slippage_bps']} bps over "
        f"{slippage['exit_fills']} fills vs modelled "
        f"{slippage['modelled_exit_slippage_bps']} bps",
        "",
        f"Round trip: realised {round_trip['realised_round_trip_bps']} bps vs "
        f"modelled {round_trip['modelled_round_trip_bps']} bps",
        "",
        "Hypotheses:",
    ]
    for item in report["hypotheses"]:
        lines.append(f"- {item['hypothesis']}: {item['reading']}")
        lines.append(f"  would test: {item['would_test']}")
    if not report["hypotheses"]:
        lines.append("- none: the modelled cost is consistent with the fills")
    return "\n".join(lines)


async def _load_report(database_url: str, days: int, limit: int) -> dict[str, Any]:
    from ..config import Settings
    from ..storage.db import Database
    from ..storage.repository import Repository

    settings = Settings()
    db = Database(database_url)
    try:
        report = await load(
            Repository(db),
            days=days,
            limit=limit,
            model=CostModel.from_settings(settings),
        )
    finally:
        await db.dispose()
    return report.as_dict()


def main(argv: list[str] | None = None) -> None:
    import argparse
    import asyncio
    import json

    from ..config import Settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--json", action="store_true", help="print the raw report")
    args = parser.parse_args(argv)
    report = asyncio.run(
        _load_report(Settings().database_url, max(1, args.days), max(1, args.limit))
    )
    print(json.dumps(report, indent=2) if args.json else format_report(report))


if __name__ == "__main__":
    main()
