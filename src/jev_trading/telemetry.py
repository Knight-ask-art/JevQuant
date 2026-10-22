"""Execution telemetry over the recorded pipeline.

    python -m jev_trading.telemetry [--days 7] [--json]

Reads the signal state trail and the adapters' own venue-call measurements,
and reports how long each pipeline stage actually took. It writes nothing and
changes nothing: the numbers are evidence for a decision, not the decision.

The stages are derived from ``signal_events``, so this works retroactively on
history recorded before the report existed. Only the venue call needed a new
measurement, and an adapter that did not make one contributes no sample rather
than a zero.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from .services.telemetry import ExecutionTelemetry, format_report


async def _run(days: int) -> dict[str, Any]:
    from .config import Settings
    from .storage.db import Database
    from .storage.repository import Repository

    settings = Settings()
    end = datetime.now(UTC)
    start = end - timedelta(days=max(1, days))
    db = Database(settings.database_url)
    try:
        repo = Repository(db)
        signals = await repo.list_signals_with_events(since=start, until=end)
        latencies = await repo.list_execution_latencies()
    finally:
        await db.dispose()
    return ExecutionTelemetry(signals=signals, venue_latencies_ms=latencies).report()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--json", action="store_true", help="print the raw report")
    args = parser.parse_args(argv)
    report = asyncio.run(_run(args.days))
    print(json.dumps(report, indent=2, default=str) if args.json else format_report(report))


if __name__ == "__main__":
    main()
