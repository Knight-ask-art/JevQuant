"""Regression tests for walk-forward's recorded exit and cost accounting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from jev_trading.services.walk_forward import net_bps
from jev_trading.walk_forward import load_samples


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    async def list_walk_forward_rows(self, *, since, until):
        return self.rows


def _row(**overrides):
    fields = dict(
        decided_at=datetime(2026, 10, 1, tzinfo=UTC),
        symbol="BTCUSDT",
        entry_price=Decimal("100"),
        # Recorder already applied exit slippage to this actual exit price.
        exit_price=Decimal("100.32"),
        net_bps=Decimal("12"),
        round_trip_cost_bps=Decimal("28"),
        gap_seconds=0,
        entry_probability=0.8,
        mfe_bps=Decimal("10"),
        mae_bps=Decimal("-10"),
        exit_reason="timeout",
        horizon_seconds=3600,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.mark.asyncio
async def test_loader_keeps_recorded_slipped_exit_price_and_reproduces_net():
    at = datetime(2026, 10, 1, tzinfo=UTC)
    samples = await load_samples(
        _Rows([_row()]), start=at, end=at + timedelta(days=1), symbols=()
    )
    assert len(samples) == 1
    assert samples[0].exit_close == Decimal("100.32")
    # Gross 32 bps, less two 10 bps taker fees = recorded net 12 bps.
    # The 8 bps exit slippage is already included in 100.32.
    assert net_bps(
        samples[0],
        stop_bps=Decimal("50"),
        take_bps=Decimal("50"),
        hold_minutes=60,
        exit_slippage_bps=Decimal("8"),
        fee_bps=Decimal("10"),
    ) == Decimal("12")


@pytest.mark.asyncio
async def test_loader_rejects_missing_or_invalid_exit_prices():
    at = datetime(2026, 10, 1, tzinfo=UTC)
    samples = await load_samples(
        _Rows([_row(exit_price=None), _row(exit_price=Decimal("0"))]),
        start=at,
        end=at + timedelta(days=1),
        symbols=(),
    )
    assert samples == []
