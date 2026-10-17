"""Persistence precision and the hash-linked audit trail."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import update

from jev_trading.domain.enums import Mode
from jev_trading.services.audit import AuditService
from jev_trading.storage.db import Database
from jev_trading.storage.models import AuditEventRow
from jev_trading.storage.repository import Repository


@pytest.fixture
async def database(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await db.create_all()
    try:
        yield db
    finally:
        await db.dispose()


async def test_decimal_round_trip_is_exact(database):
    repo = Repository(database)
    account = await repo.get_or_create_account(
        "main", Mode.PAPER, Decimal("0.1") + Decimal("0.2")
    )
    fetched = await repo.get_account(account.id)
    assert fetched.starting_equity_usdt == Decimal("0.3")
    assert str(fetched.starting_equity_usdt) == "0.3"


async def test_audit_chain_is_valid_and_detects_tampering(database):
    repo = Repository(database)
    audit = AuditService(repo)
    first = await audit.record("system.started", "system", None, {"mode": "paper"})
    await audit.record("policy.activated", "policy", "p1", {"clamps": []})

    result = await repo.verify_audit_chain()
    assert result["valid"] is True
    assert result["count"] == 2
    assert result["head"] == (await repo.list_audit(limit=1))[0].event_hash

    async with database.session() as session:
        await session.execute(
            update(AuditEventRow)
            .where(AuditEventRow.seq == first.seq)
            .values(payload_json={"mode": "live"})
        )
    tampered = await repo.verify_audit_chain()
    assert tampered["valid"] is False
    assert tampered["broken_seq"] == first.seq


async def test_secret_shaped_payloads_are_redacted(database):
    repo = Repository(database)
    audit = AuditService(repo)
    row = await audit.record(
        "connection.tested",
        "system",
        None,
        {
            "api_key": "super-secret",
            "authorization": "Bearer abc",
            "nested": {"api_secret": "xyz", "safe": "visible"},
        },
    )
    payload = row.payload_json
    assert payload["api_key"] != "super-secret"
    assert payload["authorization"] == "[REDACTED]"
    assert payload["nested"]["api_secret"] == "[REDACTED]"
    assert payload["nested"]["safe"] == "visible"
