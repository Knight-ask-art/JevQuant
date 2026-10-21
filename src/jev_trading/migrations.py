"""Forward-only schema migrations.

``create_all`` creates missing tables but never alters existing ones, so every
column added after the first release needs an entry here. Migrations are
written in portable SQLAlchemy DDL and run once per database; applied versions
are recorded in ``schema_migrations`` with a checksum of the source so an
edited, already-applied migration is detected instead of silently ignored.

Usage:
    jev migrate            # apply pending migrations (CLI)
    python -m jev_trading.migrations            # same
    python -m jev_trading.migrations --dry-run  # list without applying
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Awaitable, Callable

from sqlalchemy import inspect as sa_inspect, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from .storage.db import Database
from .storage.models import SchemaMigrationRow


@dataclass(frozen=True)
class Migration:
    version: str
    description: str
    up: Callable[[AsyncConnection], Awaitable[None]]

    @property
    def checksum(self) -> str:
        source = inspect.getsource(self.up)
        return hashlib.sha256(f"{self.up.__name__}:{source}".encode("utf-8")).hexdigest()


async def _has_table(conn: AsyncConnection, name: str) -> bool:
    return await conn.run_sync(lambda sync_conn: sa_inspect(sync_conn).has_table(name))


async def _has_column(conn: AsyncConnection, table: str, column: str) -> bool:
    if not await _has_table(conn, table):
        return False
    columns = await conn.run_sync(
        lambda sync_conn: {
            col["name"] for col in sa_inspect(sync_conn).get_columns(table)
        }
    )
    return column in columns


async def _has_index(conn: AsyncConnection, table: str, name: str) -> bool:
    if not await _has_table(conn, table):
        return False
    indexes = await conn.run_sync(
        lambda sync_conn: {idx["name"] for idx in sa_inspect(sync_conn).get_indexes(table)}
    )
    return name in indexes


async def _add_column(
    conn: AsyncConnection, table: str, name: str, type_sql: str, null: bool = True
) -> None:
    if await _has_column(conn, table, name):
        return
    nullability = "" if null else " NOT NULL"
    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {type_sql}{nullability}"))


async def up_0001_baseline(conn: AsyncConnection) -> None:
    """Baseline: prior releases created their tables via create_all."""

    # A database with no schema at all is fresh: create_all has run, or the
    # tables are created right after this baseline is recorded.


async def up_0002_positions_mode(conn: AsyncConnection) -> None:
    """Every position records the execution route that owns it."""

    await _add_column(conn, "positions", "mode", "VARCHAR(16)", null=True)
    if await _has_column(conn, "positions", "mode"):
        # Existing rows were all paper positions before live routing existed.
        await conn.execute(
            text("UPDATE positions SET mode = 'paper' WHERE mode IS NULL")
        )


async def up_0003_fills_external_id(conn: AsyncConnection) -> None:
    """Venue trade ids so reconciled fills are counted exactly once."""

    await _add_column(conn, "fills", "external_id", "VARCHAR(64)", null=True)
    if not await _has_index(conn, "fills", "uq_fills_order_external"):
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_fills_order_external "
                "ON fills (order_id, external_id)"
            )
        )


async def up_0004_positions_exit_request(conn: AsyncConnection) -> None:
    """An operator can request an immediate close of one position."""

    await _add_column(conn, "positions", "exit_requested_at", "TIMESTAMP WITH TIME ZONE", null=True)
    await _add_column(conn, "positions", "exit_requested_reason", "VARCHAR(32)", null=True)


async def up_0005_counterfactual_path(
    conn: AsyncConnection,
) -> None:
    """Counterfactual replays also record the price path and data quality."""

    for name, type_sql in (
        ("mfe_bps", "NUMERIC(28, 12)"),
        ("mae_bps", "NUMERIC(28, 12)"),
        ("holding_seconds", "INTEGER"),
        ("gap_seconds", "INTEGER"),
        ("is_synthetic", "BOOLEAN"),
    ):
        await _add_column(conn, "counterfactual_outcomes", name, type_sql)


async def up_0006_counterfactual_timing(
    conn: AsyncConnection,
) -> None:
    """Counterfactual replays also record when the path peaked and cost paid."""

    for name, type_sql in (
        # Seconds from the signal to the bar that set MFE / MAE. Together with
        # mfe_bps they answer "did price ever pay, and when" rather than only
        # "where did it end".
        ("time_to_mfe_seconds", "INTEGER"),
        ("time_to_mae_seconds", "INTEGER"),
        # Round-trip cost charged to this replay, in bps. Kept as a column so
        # gross and net can be compared without re-deriving the fee schedule
        # that was in force when the row was written.
        ("round_trip_cost_bps", "NUMERIC(28, 12)"),
    ):
        await _add_column(conn, "counterfactual_outcomes", name, type_sql)


async def up_0007_fill_fee_amount(
    conn: AsyncConnection,
) -> None:
    """Fills keep the commission in the asset the venue charged it in.

    ``fee_quote`` alone loses a spot buy's fee: the venue takes it out of the
    base asset, so a reader that only understands quote fees saw zero and the
    ledger understated costs by one side of the round trip. Existing rows are
    backfilled with ``fee_quote``, which is the correct amount whenever that
    column was populated.
    """

    await _add_column(conn, "fills", "fee_amount", "NUMERIC(28, 12)")
    if await _has_column(conn, "fills", "fee_amount"):
        await conn.execute(
            text("UPDATE fills SET fee_amount = fee_quote WHERE fee_amount IS NULL")
        )


MIGRATIONS: tuple[Migration, ...] = (
    Migration("0001_baseline", "record the pre-migration schema", up_0001_baseline),
    Migration("0002_positions_mode", "positions carry their execution route", up_0002_positions_mode),
    Migration(
        "0003_fills_external_id",
        "fills carry the venue trade id (idempotent reconciliation)",
        up_0003_fills_external_id,
    ),
    Migration(
        "0004_positions_exit_request",
        "positions record an operator-requested close",
        up_0004_positions_exit_request,
    ),
    Migration(
        "0005_counterfactual_path",
        "counterfactual replays record MFE/MAE, holding time and data gaps",
        up_0005_counterfactual_path,
    ),
    Migration(
        "0006_counterfactual_timing",
        "counterfactual replays record time-to-MFE/MAE and the cost charged",
        up_0006_counterfactual_timing,
    ),
    Migration(
        "0007_fill_fee_amount",
        "fills keep the commission in the asset the venue charged it in",
        up_0007_fill_fee_amount,
    ),
)


@dataclass
class MigrationOutcome:
    version: str
    status: str  # applied | skipped | checksum_mismatch
    detail: str = ""


async def _ensure_migration_table(conn: AsyncConnection) -> None:
    if not await _has_table(conn, "schema_migrations"):
        await conn.run_sync(
            lambda sync_conn: SchemaMigrationRow.__table__.create(sync_conn, checkfirst=True)
        )


async def applied_versions(conn: AsyncConnection) -> dict[str, str]:
    await _ensure_migration_table(conn)
    rows = (
        await conn.execute(select(SchemaMigrationRow.version, SchemaMigrationRow.checksum))
    ).all()
    return {row[0]: row[1] for row in rows}


async def pending(conn: AsyncConnection) -> list[Migration]:
    done = await applied_versions(conn)
    return [migration for migration in MIGRATIONS if migration.version not in done]


async def migrate(db: Database, *, dry_run: bool = False) -> list[MigrationOutcome]:
    """Apply every pending migration in order. Returns one outcome per migration."""

    outcomes: list[MigrationOutcome] = []
    await db.create_all()
    async with db.engine.begin() as conn:
        done = await applied_versions(conn)
        for migration in MIGRATIONS:
            recorded = done.get(migration.version)
            if recorded is not None:
                status = (
                    "checksum_mismatch"
                    if recorded != migration.checksum
                    else "skipped"
                )
                outcomes.append(MigrationOutcome(migration.version, status))
                continue
            if dry_run:
                outcomes.append(MigrationOutcome(migration.version, "pending"))
                continue
            await migration.up(conn)
            await conn.execute(
                SchemaMigrationRow.__table__.insert().values(
                    version=migration.version,
                    checksum=migration.checksum,
                    applied_at=datetime.now(UTC),
                )
            )
            outcomes.append(MigrationOutcome(migration.version, "applied"))
    return outcomes


async def run(database_url: str, *, dry_run: bool = False) -> list[MigrationOutcome]:
    db = Database(database_url)
    try:
        return await migrate(db, dry_run=dry_run)
    finally:
        await db.dispose()


def main(argv: list[str] | None = None) -> int:
    from .config import get_settings

    parser = argparse.ArgumentParser(description="Apply database schema migrations.")
    parser.add_argument("--db", default=None, help="Override JEV_DATABASE_URL.")
    parser.add_argument("--dry-run", action="store_true", help="List pending migrations only.")
    args = parser.parse_args(argv)
    settings = get_settings()
    url = args.db or settings.database_url
    outcomes: list[MigrationOutcome] = asyncio.run(run(url, dry_run=args.dry_run))
    for outcome in outcomes:
        line = f"{outcome.version:<26} {outcome.status}"
        print(line if not outcome.detail else f"{line}  {outcome.detail}")
    if any(outcome.status == "checksum_mismatch" for outcome in outcomes):
        print("A migration changed after it was applied; review before continuing.")
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
