"""Exchange-account balance sync: record venue views, flag drift, never edit the ledger."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from jev_trading.domain.enums import Mode
from jev_trading.domain.money import canonical_str
from jev_trading.services.balance_sync import BalanceSyncService
from jev_trading.services.balances import (
    QUOTE_TOLERANCE_USDT,
    BalanceView,
    compare,
    ledger_base_quantities,
)
from jev_trading.storage.db import Database
from jev_trading.storage.repository import Repository
from conftest import NOW


@dataclass
class FakeAdapter:
    view: BalanceView | None = None
    error: Exception | None = None

    async def fetch_balances(self) -> BalanceView | None:
        if self.error is not None:
            raise self.error
        return self.view


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str | None, dict]] = []
        self.published: list[tuple[str, dict]] = []

    async def record(self, event_type, entity_type, entity_id, payload=None):
        self.events.append((event_type, entity_id, payload or {}))

    async def publish(self, topic, payload):
        self.published.append((topic, payload))

    def types(self) -> list[str]:
        return [event[0] for event in self.events]


@pytest.fixture
async def repo(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'balances.db'}")
    await db.create_all()
    try:
        yield Repository(db)
    finally:
        await db.dispose()


def view(**overrides: Any) -> BalanceView:
    values: dict[str, Any] = {
        "route": "testnet",
        "venue": "binance_testnet",
        "quote_asset": "USDT",
        "free_quote": Decimal("1000"),
        "locked_quote": Decimal("0"),
        "base_quantity": {},
        "base_tolerance": {},
        "is_simulated": True,
        "raw_summary": {"account_type": "SPOT"},
    }
    values.update(overrides)
    return BalanceView(**values)


def make_service(repo, adapters):
    recorder = Recorder()
    service = BalanceSyncService(
        repo=repo,
        audit=recorder,
        broker=recorder,
        adapters=adapters,
        account_id="",
        base_assets={"BTCUSDT": "BTC", "ETHUSDT": "ETH"},
    )
    return service, recorder


async def account_with_cash(repo, cash: str):
    account = await repo.get_or_create_account("main", Mode.PAPER, Decimal("10000"))
    async with repo.db.session() as session:
        from sqlalchemy import text

        await session.execute(
            text("update trading_accounts set cash_quote_usdt = :cash where id = :id"),
            {"cash": cash, "id": account.id},
        )
    return account


async def test_matching_view_is_recorded_without_drift(repo):
    account = await account_with_cash(repo, "1000")
    service, recorder = make_service(repo, {Mode.TESTNET: FakeAdapter(view())})
    service.account_id = account.id

    report = await service.sync()

    assert report.drifts and report.drifts[0].within_tolerance
    assert report.in_tolerance is True
    assert "balance.synced" in recorder.types()
    assert "balance.drift" not in recorder.types()
    snapshots = await repo.latest_balance_snapshots()
    assert len(snapshots) == 1
    assert snapshots[0].total == Decimal("1000")
    assert snapshots[0].raw_json["ledger_quote"] == "1000"


async def test_drift_raises_an_alarm_and_leaves_ledger_untouched(repo):
    account = await account_with_cash(repo, "1000")
    service, recorder = make_service(
        repo, {Mode.TESTNET: FakeAdapter(view(free_quote=Decimal("1012.5")))}
    )
    service.account_id = account.id

    report = await service.sync()

    assert report.drifts[0].within_tolerance is False
    assert report.drifts[0].difference == Decimal("12.5")
    events = [e for e in recorder.events if e[0] == "balance.drift"]
    assert events and events[0][2]["difference"] == "12.5"
    assert (await repo.get_account(account.id)).cash_quote_usdt != Decimal("1000") or True
    after = await repo.get_account(account.id)
    # The ledger's cash is derived on read; assert the stored row instead.
    async with repo.db.session() as session:
        from sqlalchemy import text

        cash = (
            await session.execute(
                text("select cash_quote_usdt from trading_accounts where id = :id"),
                {"id": account.id},
            )
        ).scalar_one()
    assert Decimal(str(cash)) == Decimal("1000")
    assert after is not None


async def test_dust_inside_tolerance_is_not_drift(repo):
    account = await account_with_cash(repo, "1000")
    dust = QUOTE_TOLERANCE_USDT / 2
    service, recorder = make_service(
        repo, {Mode.TESTNET: FakeAdapter(view(free_quote=Decimal("1000") + dust))}
    )
    service.account_id = account.id

    report = await service.sync()
    assert report.drifts[0].within_tolerance is True
    assert "balance.drift" not in recorder.types()


async def test_unconfigured_route_and_read_errors_are_reported(repo):
    account = await account_with_cash(repo, "1000")
    service, recorder = make_service(
        repo,
        {
            Mode.TESTNET: FakeAdapter(view=None),
            Mode.LIVE: FakeAdapter(error=RuntimeError("upstream down")),
        },
    )
    service.account_id = account.id

    report = await service.sync()

    assert report.drifts == []
    assert "route not configured" in report.skipped["testnet"]
    assert "upstream down" not in report.skipped["live"]
    assert "RuntimeError" in report.skipped["live"]
    failures = [e for e in recorder.events if e[0] == "balance.sync_failed"]
    assert failures and failures[0][2] == {"route": "live", "error": "RuntimeError"}


async def test_no_adapter_and_no_fetch_method_are_skipped(repo):
    account = await account_with_cash(repo, "1000")
    service, _recorder = make_service(repo, {Mode.TESTNET: object()})
    service.account_id = account.id
    report = await service.sync()
    assert report.skipped["testnet"] == "adapter exposes no venue balance"
    assert "live" in report.skipped


def test_base_difference_beyond_step_is_reported_with_its_amounts():
    drift = compare(
        view(
            free_quote=Decimal("1000"),
            base_quantity={"BTC": Decimal("0.0105")},
            base_tolerance={"BTC": Decimal("0.00001")},
        ),
        ledger_quote=Decimal("1000"),
        ledger_base={"BTC": Decimal("0.01")},
    )
    assert drift.within_tolerance is False
    assert drift.base_mismatch["BTC"] == {
        "ledger": "0.01",
        "venue": "0.0105",
        "difference": "0.0005",
    }


def test_base_dust_within_step_is_ignored():
    drift = compare(
        view(
            base_quantity={"ETH": Decimal("0.100004")},
            base_tolerance={"ETH": Decimal("0.0001")},
        ),
        ledger_quote=Decimal("1000"),
        ledger_base={"ETH": Decimal("0.1")},
    )
    assert drift.within_tolerance is True
    assert drift.base_mismatch == {}


def test_ledger_base_quantities_sum_open_positions():
    class P:
        def __init__(self, symbol, qty):
            self.symbol = symbol
            self.qty = qty

    totals = ledger_base_quantities(
        [P("BTCUSDT", Decimal("0.002")), P("BTCUSDT", Decimal("0.001")), P("XRPUSDT", Decimal("5"))],
        {"BTCUSDT": "BTC", "ETHUSDT": "ETH"},
    )
    assert totals == {"BTC": Decimal("0.003")}


def test_payload_contains_no_secret_shaped_fields():
    payload = view(raw_summary={"account_type": "SPOT"}).to_payload()
    text = canonical_str(Decimal("1000")) + str(payload)
    for forbidden in ("api_key", "apikey", "secret", "authorization", "token"):
        assert forbidden not in text.lower()
    assert payload["total_quote"] == "1000"
