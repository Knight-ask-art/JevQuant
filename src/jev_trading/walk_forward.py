"""Walk-forward over the recorded outcomes, as a command.

    python -m jev_trading.walk_forward --days 28 [--symbols BTCUSDT,ETHUSDT] [--json]

Reads the counterfactual record (including its actual modelled exit price), splits it into rolling
train/validation/test windows, searches a stop/take/hold grid for a *plateau*
on each training window, and reports what that plateau did on the segment
nobody looked at. It writes nothing: a parameter is chosen by a person, with
the fold record in hand.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from .domain.costs import CostModel
from .services.walk_forward import (
    DEFAULT_MIN_TRADES,
    Sample,
    build_report,
    format_report,
)


async def load_samples(
    repo: Any, *, start: datetime, end: datetime, symbols: tuple[str, ...]
) -> list[Sample]:
    rows = await repo.list_walk_forward_rows(since=start, until=end)
    wanted = {symbol.upper() for symbol in symbols}
    out: list[Sample] = []
    for row in rows:
        if wanted and row.symbol.upper() not in wanted:
            continue
        if row.net_bps is None or row.gap_seconds or row.exit_price is None:
            # A window the replay never fully saw cannot be repriced, and a row
            # with no path is not a sample: it is a missing measurement.
            continue
        entry = Decimal(str(row.entry_price))
        exit_price = Decimal(str(row.exit_price))
        if entry <= 0 or exit_price <= 0:
            continue
        out.append(
            Sample(
                decided_at=row.decided_at,
                symbol=row.symbol,
                entry_price=entry,
                entry_close=entry,
                exit_close=exit_price,
                entry_probability=float(row.entry_probability or 0.0),
                mfe_bps=Decimal(str(row.mfe_bps or 0)),
                mae_bps=Decimal(str(row.mae_bps or 0)),
                recorded_exit_reason=row.exit_reason,
                window_minutes=max(1, int(row.horizon_seconds) // 60),
                round_trip_cost_bps=Decimal(str(row.round_trip_cost_bps or 0)),
                recorded_net_bps=Decimal(str(row.net_bps)),
            )
        )
    return out


async def _run(
    days: int, symbols: tuple[str, ...], fold_count: int, min_trades: int
) -> dict[str, Any]:
    from .config import Settings
    from .storage.db import Database
    from .storage.repository import Repository

    settings = Settings()
    end = datetime.now(UTC)
    start = end - timedelta(days=max(1, days))
    cost = CostModel.from_settings(settings)
    db = Database(settings.database_url)
    try:
        repo = Repository(db)
        samples = await load_samples(repo, start=start, end=end, symbols=symbols)
    finally:
        await db.dispose()
    horizon = timedelta(minutes=max((s.window_minutes for s in samples), default=60))
    return build_report(
        samples,
        start=start,
        end=end,
        horizon=horizon,
        cost=cost,
        fold_count=max(1, fold_count),
        min_trades=max(2, min_trades),
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--symbols", default="")
    parser.add_argument("--folds", type=int, default=4)
    parser.add_argument("--min-trades", type=int, default=DEFAULT_MIN_TRADES)
    parser.add_argument("--json", action="store_true", help="print the raw report")
    args = parser.parse_args(argv)
    report = asyncio.run(
        _run(
            args.days,
            tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip()),
            args.folds,
            args.min_trades,
        )
    )
    print(json.dumps(report, indent=2, default=str) if args.json else format_report(report))


if __name__ == "__main__":
    main()
