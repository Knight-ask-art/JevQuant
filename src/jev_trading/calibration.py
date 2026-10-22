"""Jev calibration report over the recorded outcomes.

    python -m jev_trading.calibration [--min-samples 100] [--json]

Groups recorded candidates by (model, exit schema), fits a Platt calibration on
the early part of each cohort, and reports the held-out check. With too little
history every cohort comes back ``insufficient`` -- that is the honest answer,
and it is printed with the counts that would be needed.

Nothing is written and nothing is applied: the entry gate keeps comparing the
raw confidence until a calibration is checked and deliberately wired in.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .services.calibration import (
    DEFAULT_MIN_SAMPLES,
    CalibrationSample,
    calibrate,
    format_report,
    is_win,
    label_from_outcomes,
)

MODEL_UNKNOWN = "unknown-model"
EXITS_UNKNOWN = "unknown-exits"


def cohort_of(row: Any) -> str:
    """(model, exit schema) -- the two things a win rate depends on.

    The exit schema is the holding window in minutes: a 60-minute hold and a
    24-hour hold are different bets, and pooling them would calibrate neither.
    """

    return f"{row.model or MODEL_UNKNOWN}|exit-{max(1, int(row.horizon_seconds) // 60)}m"


def build_samples(rows: list[Any]) -> dict[str, list[CalibrationSample]]:
    cohorts: dict[str, list[CalibrationSample]] = defaultdict(list)
    for row in label_from_outcomes(rows):
        if row.confidence is None:
            # A signal whose evaluation is gone has no score to calibrate;
            # inventing one would put it in the wrong bin.
            continue
        cohorts[cohort_of(row)].append(
            CalibrationSample(
                confidence=float(row.confidence),
                won=is_win(row),
                decided_at=row.decided_at,
            )
        )
    return dict(cohorts)


def build_report(
    rows: list[Any], *, min_samples: int = DEFAULT_MIN_SAMPLES, now: datetime | None = None
) -> dict[str, Any]:
    fitted_at = now or datetime.now(timezone.utc)
    cohorts = build_samples(rows)
    return {
        "generated_at": fitted_at.isoformat(),
        "rows_read": len(rows),
        "cohorts": {
            name: calibrate(samples, cohort=name, min_samples=min_samples, fitted_at=fitted_at)
            for name, samples in sorted(cohorts.items())
        },
    }


def format_cohorts(report: dict[str, Any]) -> str:
    if not report["cohorts"]:
        return "No decided outcomes with a recorded score yet."
    return "\n\n".join(format_report(item) for item in report["cohorts"].values())


async def _run(min_samples: int) -> dict[str, Any]:
    from .config import Settings
    from .storage.db import Database
    from .storage.repository import Repository

    settings = Settings()
    db = Database(settings.database_url)
    try:
        rows = await Repository(db).list_calibration_rows()
    finally:
        await db.dispose()
    return build_report(rows, min_samples=max(2, min_samples))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES)
    parser.add_argument("--json", action="store_true", help="print the raw report")
    args = parser.parse_args(argv)
    report = asyncio.run(_run(args.min_samples))
    print(json.dumps(report, indent=2, default=str) if args.json else format_cohorts(report))


if __name__ == "__main__":
    main()
