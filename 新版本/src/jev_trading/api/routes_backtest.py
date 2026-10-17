"""Backtest submission and retrieval."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..domain.enums import ErrorCode
from ..domain.errors import AppError
from ..services.backtest import BacktestSpec, run_backtest
from . import serializers as ser
from .security import require_operator

router = APIRouter(prefix="/api/v1/backtests", tags=["backtests"])


def _container(request: Request):
    return request.app.state.container


class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT"], min_length=1)
    minutes: int = Field(default=5, ge=1, le=1440)
    candles: int = Field(default=1_500, ge=100, le=20_000)
    starting_equity_usdt: str = "10000"
    max_position_notional_usdt: str = "500"
    stop_loss_bps: str = "120"
    take_profit_bps: str = "240"
    max_holding_minutes: int = Field(default=240, ge=1)
    fee_bps: str = "10"
    slippage_bps: str = "2"
    seed: int = 42


@router.post("")
async def create_backtest(
    request: Request,
    payload: BacktestRequest,
    actor: str = Depends(require_operator),
) -> dict[str, object]:
    """Run a deterministic replay to completion and persist the report.

    The engine is intentionally synchronous and bounded (max 20k candles per
    symbol); it runs in a worker thread so the event loop stays responsive.
    """

    container = _container(request)
    allowed = set(container.settings.allowed_symbols)
    symbols = [symbol.strip().upper() for symbol in payload.symbols]
    unknown = sorted(set(symbols) - allowed)
    if unknown:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"Symbols outside the allowed universe: {', '.join(unknown)}.",
        )
    try:
        spec = BacktestSpec.from_payload(
            {**payload.model_dump(), "symbols": symbols}
        )
    except Exception as exc:  # noqa: BLE001
        raise AppError(
            ErrorCode.VALIDATION_ERROR, f"Invalid backtest parameters: {exc}"
        ) from exc

    row = await container.repo.create_backtest_run(
        {"spec": spec.as_dict(), "actor": actor, "engine": "deterministic_replay_v1"}
    )
    await container.repo.update_backtest_run(row.id, status="RUNNING")
    try:
        report = await asyncio.to_thread(run_backtest, spec)
    except Exception as exc:  # noqa: BLE001
        await container.repo.update_backtest_run(
            row.id, status="FAILED", error_code=type(exc).__name__
        )
        raise AppError(
            ErrorCode.CONFLICT, f"Backtest failed: {type(exc).__name__}"
        ) from exc
    updated = await container.repo.update_backtest_run(
        row.id, status="COMPLETED", result=report
    )
    await container.audit.record(
        "backtest.completed",
        "backtest",
        row.id,
        {
            "symbols": symbols,
            "trades": report["summary"]["trades"],
            "net_pnl_usdt": report["summary"]["net_pnl_usdt"],
            "is_synthetic": True,
            "actor": actor,
        },
    )
    return ser.backtest(updated)


@router.get("")
async def list_backtests(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    actor: str = Depends(require_operator),
) -> dict[str, object]:
    container = _container(request)
    rows = await container.repo.list_backtest_runs(limit=limit)
    return {"items": [ser.backtest(row) for row in rows]}


@router.get("/{run_id}")
async def get_backtest(
    run_id: str, request: Request, actor: str = Depends(require_operator)
) -> dict[str, object]:
    container = _container(request)
    row = await container.repo.get_backtest_run(run_id)
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, f"Backtest {run_id} was not found.")
    return ser.backtest(row)
