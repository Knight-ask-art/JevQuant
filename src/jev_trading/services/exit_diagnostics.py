"""Read-only diagnostics over recorded outcomes: where did the money go?

The question this answers is not "what should the exit rules be". It is
"which hypothesis does the recorded path support", so that a parameter change
is made for a stated reason instead of from four unlucky trades.

Two distinctions do most of the work:

- **Never paid vs paid and gave it back.** A timeout whose MFE never covered
  the round-trip cost says the market never offered a profit; no exit rule can
  fix that. A timeout with a large MFE and a small realised value says the
  exit geometry lost money the market did offer.
- **Path timing.** ``time_to_mfe_seconds`` says how long the profit took to
  appear, which is what a holding-time or trailing decision actually depends
  on.

Nothing here mutates policy, places orders, or feeds back into the gate. It
produces numbers and named hypotheses; choosing one is a human decision.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Any

ZERO = Decimal("0")

#: A timeout that never reached this fraction of the cost it would have paid is
#: treated as "the market never offered a trade", not as a bad exit.
COST_COVERAGE_FLOOR = Decimal("1")
#: An MFE this large that still ended in a timeout is a giveback candidate.
GIVEBACK_FLOOR_BPS = Decimal("40")


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _median(values: Iterable[Decimal | None]) -> float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return round(float(median(present)), 2)


def _rate(count: int, total: int) -> float | None:
    return round(count / total, 4) if total else None


@dataclass(frozen=True)
class ExitDiagnostics:
    """Aggregate read of one set of replayed outcomes."""

    rows: Sequence[Any]

    def _decided(self) -> list[Any]:
        return [row for row in self.rows if _dec(row.net_bps) is not None]

    def by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.rows:
            counts[row.exit_reason] = counts.get(row.exit_reason, 0) + 1
        return dict(sorted(counts.items()))

    def timeout_report(self) -> dict[str, Any]:
        """Split timeouts into "never paid" and "paid then gave it back"."""

        timeouts = [row for row in self._decided() if row.exit_reason == "timeout"]
        if not timeouts:
            return {"timeouts": 0}

        never_paid = []
        gave_back = []
        for row in timeouts:
            mfe = _dec(row.mfe_bps)
            cost = _dec(row.round_trip_cost_bps)
            if mfe is None:
                continue
            if cost is not None and mfe < cost * COST_COVERAGE_FLOOR:
                never_paid.append(row)
            elif mfe >= GIVEBACK_FLOOR_BPS:
                gave_back.append(row)

        return {
            "timeouts": len(timeouts),
            "median_mfe_bps": _median(_dec(row.mfe_bps) for row in timeouts),
            "median_mae_bps": _median(_dec(row.mae_bps) for row in timeouts),
            "median_holding_seconds": _median(
                Decimal(row.holding_seconds) if row.holding_seconds is not None else None
                for row in timeouts
            ),
            "never_covered_cost": len(never_paid),
            "never_covered_cost_rate": _rate(len(never_paid), len(timeouts)),
            "peaked_above_giveback_floor": len(gave_back),
            "peaked_above_giveback_floor_rate": _rate(len(gave_back), len(timeouts)),
            "median_time_to_mfe_seconds": _median(
                Decimal(row.time_to_mfe_seconds)
                if row.time_to_mfe_seconds is not None
                else None
                for row in timeouts
            ),
        }

    def capture_report(self) -> dict[str, Any]:
        """How much of the best available move each exit actually kept.

        ``capture_ratio`` is realised gross over MFE: 1.0 means the exit took
        everything the window offered, 0 means it gave all of it back. Rows
        whose MFE is not above zero are excluded, since a ratio against a
        non-positive peak is not meaningful.
        """

        ratios = []
        givebacks = []
        for row in self._decided():
            mfe = _dec(row.mfe_bps)
            net = _dec(row.net_bps)
            cost = _dec(row.round_trip_cost_bps) or ZERO
            if mfe is None or mfe <= ZERO or net is None:
                continue
            gross = net + cost
            ratios.append(gross / mfe)
            givebacks.append(mfe - gross)
        return {
            "rows_with_positive_mfe": len(ratios),
            "median_capture_ratio": round(float(median(ratios)), 4) if ratios else None,
            "median_giveback_bps": _median(givebacks),
        }

    def cost_report(self) -> dict[str, Any]:
        decided = self._decided()
        costs = [_dec(row.round_trip_cost_bps) for row in decided]
        nets = [_dec(row.net_bps) for row in decided]
        gross = [
            net + cost
            for net, cost in zip(nets, costs)
            if net is not None and cost is not None
        ]
        return {
            "median_round_trip_cost_bps": _median(costs),
            "average_gross_bps": round(float(sum(gross, ZERO) / len(gross)), 2)
            if gross
            else None,
            "average_net_bps": round(float(sum(nets, ZERO) / len(nets)), 2)
            if nets
            else None,
        }

    def hypotheses(self) -> list[dict[str, str]]:
        """Named readings of the data. Evidence, not a decision.

        Each entry says what was observed and which experiment it would
        justify; none of them changes a policy on its own.
        """

        out: list[dict[str, str]] = []
        timeout = self.timeout_report()
        if timeout.get("timeouts"):
            rate = timeout.get("never_covered_cost_rate")
            if rate is not None and rate >= 0.6:
                out.append(
                    {
                        "hypothesis": "market_never_offered_the_trade",
                        "reading": (
                            f"{timeout['never_covered_cost']} of {timeout['timeouts']} "
                            "timeouts never reached their round-trip cost"
                        ),
                        "would_test": "a no-trade regime filter, not a new exit rule",
                    }
                )
            giveback_rate = timeout.get("peaked_above_giveback_floor_rate")
            if giveback_rate is not None and giveback_rate >= 0.3:
                out.append(
                    {
                        "hypothesis": "profit_was_available_and_given_back",
                        "reading": (
                            f"{timeout['peaked_above_giveback_floor']} of "
                            f"{timeout['timeouts']} timeouts peaked above "
                            f"{int(GIVEBACK_FLOOR_BPS)} bps and still timed out"
                        ),
                        "would_test": "trailing / break-even / time-decay exits",
                    }
                )
        capture = self.capture_report()
        ratio = capture.get("median_capture_ratio")
        if ratio is not None and ratio < 0.5:
            out.append(
                {
                    "hypothesis": "exit_geometry_keeps_less_than_half_of_the_move",
                    "reading": f"median capture ratio {ratio}",
                    "would_test": "tighter target or trailing exit, compared pairwise",
                }
            )
        cost = self.cost_report()
        if cost["average_gross_bps"] is not None and cost["average_net_bps"] is not None:
            if cost["average_gross_bps"] > 0 >= cost["average_net_bps"]:
                out.append(
                    {
                        "hypothesis": "cost_eats_the_whole_edge",
                        "reading": (
                            f"gross {cost['average_gross_bps']} bps, "
                            f"net {cost['average_net_bps']} bps"
                        ),
                        "would_test": "maker entry / lower fee tier before any model change",
                    }
                )
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows": len(self.rows),
            "decided": len(self._decided()),
            "by_reason": self.by_reason(),
            "timeout": self.timeout_report(),
            "capture": self.capture_report(),
            "cost": self.cost_report(),
            "hypotheses": self.hypotheses(),
        }


async def load(repo: Any, *, limit: int = 500) -> ExitDiagnostics:
    return ExitDiagnostics(rows=await repo.list_counterfactuals(limit=limit))


def format_report(report: dict[str, Any]) -> str:
    lines = [
        f"rows: {report['rows']}  decided: {report['decided']}",
        f"exit reasons: {report['by_reason']}",
        "",
        "Timeout path:",
    ]
    timeout = report["timeout"]
    if timeout.get("timeouts"):
        lines.append(
            f"- {timeout['timeouts']} timeouts, median MFE {timeout['median_mfe_bps']} bps, "
            f"median MAE {timeout['median_mae_bps']} bps"
        )
        lines.append(
            f"- never covered cost: {timeout['never_covered_cost']} "
            f"({timeout['never_covered_cost_rate']})"
        )
        lines.append(
            f"- peaked above {int(GIVEBACK_FLOOR_BPS)} bps: "
            f"{timeout['peaked_above_giveback_floor']} "
            f"({timeout['peaked_above_giveback_floor_rate']})"
        )
    else:
        lines.append("- none")
    capture = report["capture"]
    lines.append("")
    lines.append(
        f"Capture: median ratio {capture['median_capture_ratio']} over "
        f"{capture['rows_with_positive_mfe']} rows with positive MFE, "
        f"median giveback {capture['median_giveback_bps']} bps"
    )
    cost = report["cost"]
    lines.append(
        f"Cost: median round trip {cost['median_round_trip_cost_bps']} bps, "
        f"average gross {cost['average_gross_bps']} bps -> "
        f"net {cost['average_net_bps']} bps"
    )
    lines.append("")
    if report["hypotheses"]:
        lines.append("Hypotheses supported by this data:")
        for item in report["hypotheses"]:
            lines.append(f"- {item['hypothesis']}: {item['reading']} -> {item['would_test']}")
    else:
        lines.append("No hypothesis is supported strongly enough to act on yet.")
    return "\n".join(lines)


async def _load_report(database_url: str, limit: int) -> dict[str, Any]:
    from ..storage.db import Database
    from ..storage.repository import Repository

    database = Database(database_url)
    try:
        await database.create_all()
        diagnostics = await load(Repository(database), limit=limit)
        return diagnostics.as_dict()
    finally:
        await database.dispose()


def main(argv: list[str] | None = None) -> None:
    import argparse
    import asyncio
    import json

    from ..config import Settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--json", action="store_true", help="print the raw report")
    args = parser.parse_args(argv)
    report = asyncio.run(_load_report(Settings().database_url, max(1, args.limit)))
    print(json.dumps(report, indent=2) if args.json else format_report(report))


if __name__ == "__main__":
    main()
