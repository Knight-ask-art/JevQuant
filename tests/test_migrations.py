"""Schema migrations upgrade an existing database without losing ledger rows."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from jev_trading.migrations import MIGRATIONS, migrate
from jev_trading.storage.db import Database


def old_schema_statements() -> list[str]:
    """The schema as it existed before route/fill-id columns were added."""

    return [
        """
        CREATE TABLE trading_accounts (
            id VARCHAR(36) PRIMARY KEY, name VARCHAR(64) UNIQUE, mode VARCHAR(16),
            starting_equity_usdt NUMERIC, cash_quote_usdt NUMERIC,
            realized_pnl_usdt NUMERIC, peak_equity_usdt NUMERIC,
            daily_realized_pnl_usdt NUMERIC, daily_pnl_date VARCHAR(10),
            created_at DATETIME, updated_at DATETIME
        )
        """,
        """
        CREATE TABLE positions (
            id VARCHAR(36) PRIMARY KEY, account_id VARCHAR(36), symbol VARCHAR(24),
            qty NUMERIC, avg_entry_price NUMERIC, stop_loss_bps NUMERIC,
            take_profit_bps NUMERIC, max_holding_seconds INTEGER, status VARCHAR(16),
            realized_pnl_usdt NUMERIC, opened_at DATETIME, closed_at DATETIME,
            entry_order_id VARCHAR(36), exit_order_id VARCHAR(36),
            policy_id VARCHAR(36), signal_id VARCHAR(36), is_simulated BOOLEAN
        )
        """,
        """
        CREATE TABLE orders (
            id VARCHAR(36) PRIMARY KEY, intent_id VARCHAR(36), exchange_order_id VARCHAR(128),
            symbol VARCHAR(24), side VARCHAR(8), order_type VARCHAR(8), qty NUMERIC,
            price NUMERIC, status VARCHAR(24), filled_qty NUMERIC,
            avg_fill_price NUMERIC, fee_quote NUMERIC, mode VARCHAR(16),
            is_simulated BOOLEAN, created_at DATETIME
        )
        """,
        """
        CREATE TABLE fills (
            id VARCHAR(36) PRIMARY KEY, order_id VARCHAR(36), qty NUMERIC,
            price NUMERIC, fee_quote NUMERIC, fee_asset VARCHAR(16), created_at DATETIME
        )
        """,
    ]


@pytest.fixture
async def legacy_db(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    async with db.engine.begin() as conn:
        for statement in old_schema_statements():
            await conn.execute(text(statement))
        await conn.execute(
            text(
                "INSERT INTO trading_accounts (id, name, mode, starting_equity_usdt, "
                "cash_quote_usdt, peak_equity_usdt) VALUES "
                "('a1', 'main', 'paper', 10000, 10000, 10000)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO positions (id, account_id, symbol, qty, avg_entry_price, "
                "stop_loss_bps, take_profit_bps, max_holding_seconds, status, "
                "realized_pnl_usdt) VALUES "
                "('p1', 'a1', 'BTCUSDT', '0.001', 68000, 100, 200, 3600, 'OPEN', 0)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO orders (id, intent_id, symbol, side, order_type, qty, "
                "status, filled_qty, avg_fill_price, fee_quote, mode, created_at) "
                "VALUES ('o1', 'i1', 'BTCUSDT', 'buy', 'LIMIT', '0.001', 'FILLED', "
                "'0.001', 68000, 0.068, 'paper', '2026-10-17 12:00:00')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO fills (id, order_id, qty, price, fee_quote, fee_asset, "
                "created_at) VALUES ('f1', 'o1', '0.001', 68000, 0.068, 'USDT', "
                "'2026-10-17 12:00:00')"
            )
        )
    try:
        yield db
    finally:
        await db.dispose()


async def test_migrate_upgrades_legacy_schema_without_losing_rows(legacy_db):
    outcomes = await migrate(legacy_db)
    statuses = {outcome.version: outcome.status for outcome in outcomes}
    assert statuses == {migration.version: "applied" for migration in MIGRATIONS}

    async with legacy_db.session() as session:
        mode = (
            await session.execute(text("select mode from positions where id='p1'"))
        ).scalar_one()
        assert mode == "paper"
        columns = {
            row[1]
            for row in (await session.execute(text("pragma table_info(fills)"))).all()
        }
        assert "external_id" in columns
        assert (
            await session.execute(text("select count(*) from fills"))
        ).scalar_one() == 1


async def test_migrate_is_idempotent(legacy_db):
    await migrate(legacy_db)
    second = await migrate(legacy_db)
    assert all(outcome.status == "skipped" for outcome in second)


async def test_duplicate_external_fill_is_rejected_by_the_index(legacy_db):
    await migrate(legacy_db)
    async with legacy_db.session() as session:
        await session.execute(
            text(
                "insert into fills (id, order_id, external_id, qty, price, fee_quote, "
                "fee_asset) values ('f2', 'o1', 'T1', 0.001, 68000, 0.07, 'USDT')"
            )
        )
    with pytest.raises(Exception):
        async with legacy_db.session() as session:
            await session.execute(
                text(
                    "insert into fills (id, order_id, external_id, qty, price, fee_quote, "
                    "fee_asset) values ('f3', 'o1', 'T1', 0.001, 68000, 0.07, 'USDT')"
                )
            )


async def test_dry_run_changes_nothing(legacy_db):
    outcomes = await migrate(legacy_db, dry_run=True)
    assert all(outcome.status == "pending" for outcome in outcomes)
    async with legacy_db.session() as session:
        columns = {
            row[1]
            for row in (await session.execute(text("pragma table_info(positions)"))).all()
        }
    assert "mode" not in columns


async def test_fresh_database_migrates_cleanly(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    try:
        first = await migrate(db)
        assert all(outcome.status == "applied" for outcome in first)
        second = await migrate(db)
        assert all(outcome.status == "skipped" for outcome in second)
    finally:
        await db.dispose()
