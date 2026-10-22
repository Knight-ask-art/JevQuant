"""Signals, orders, positions, PnL and the audit trail (read-only views)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request

from ..domain.enums import ErrorCode, SignalState
from ..domain.errors import AppError
from ..services.telemetry import ExecutionTelemetry
from . import serializers as ser
from .security import require_operator

router = APIRouter(prefix="/api/v1", tags=["activity"])


def _container(request: Request):
    return request.app.state.container


@router.get("/signals")
async def list_signals(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    state: str | None = Query(default=None),
    actor: str = Depends(require_operator),
) -> dict[str, object]:
    container = _container(request)
    parsed: SignalState | None = None
    if state:
        try:
            parsed = SignalState(state.upper())
        except ValueError as exc:
            raise AppError(
                ErrorCode.VALIDATION_ERROR, f"Unknown state {state!r}."
            ) from exc
    rows = await container.repo.list_signals(limit=limit, state=parsed)
    return {"items": [ser.signal(row) for row in rows]}


@router.get("/signals/{signal_id}")
async def signal_detail(
    signal_id: str, request: Request, actor: str = Depends(require_operator)
) -> dict[str, object]:
    container = _container(request)
    row = await container.repo.get_signal(signal_id)
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, f"Signal {signal_id} was not found.")
    events = await container.repo.get_signal_events(signal_id)
    evaluation = None
    if row.jev_evaluation_id:
        evaluation_row = await container.repo.get_jev_evaluation(row.jev_evaluation_id)
        if evaluation_row is not None:
            evaluation = ser.jev_evaluation(evaluation_row)
    intent_row = None
    risk_row = None
    intents = [
        item
        for item in await container.repo.list_intents(limit=200)
        if item.signal_id == signal_id
    ]
    if intents:
        intent_row = ser.intent(intents[0])
        decision = await container.repo.get_risk_decision_for_intent(intents[0].id)
        if decision is not None:
            risk_row = ser.risk_decision(decision)
    order_row = await container.repo.latest_order_for_signal(signal_id)
    return {
        "signal": ser.signal(row),
        "events": [ser.signal_event(event) for event in events],
        "jev_evaluation": evaluation,
        "intent": intent_row,
        "risk_decision": risk_row,
        "order": ser.order(order_row) if order_row is not None else None,
    }


@router.get("/intents")
async def list_intents(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    actor: str = Depends(require_operator),
) -> dict[str, object]:
    container = _container(request)
    rows = await container.repo.list_intents(limit=limit)
    return {"items": [ser.intent(row) for row in rows]}


@router.get("/risk-decisions")
async def list_risk_decisions(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    actor: str = Depends(require_operator),
) -> dict[str, object]:
    container = _container(request)
    rows = await container.repo.list_risk_decisions(limit=limit)
    return {"items": [ser.risk_decision(row) for row in rows]}


@router.get("/orders")
async def list_orders(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    actor: str = Depends(require_operator),
) -> dict[str, object]:
    container = _container(request)
    rows = await container.repo.list_orders(limit=limit)
    return {"items": [ser.order(row) for row in rows]}


@router.get("/orders/{order_id}")
async def order_detail(
    order_id: str, request: Request, actor: str = Depends(require_operator)
) -> dict[str, object]:
    container = _container(request)
    row = await container.repo.get_order(order_id)
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, f"Order {order_id} was not found.")
    return {"order": ser.order(row)}


@router.get("/fills")
async def list_fills(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    actor: str = Depends(require_operator),
) -> dict[str, object]:
    container = _container(request)
    rows = await container.repo.list_fills(limit=limit)
    return {"items": [ser.fill(row) for row in rows]}


@router.get("/edge-shadow")
async def list_edge_shadow(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    actor: str = Depends(require_operator),
) -> dict[str, object]:
    """What the shadow edge evaluator said, and how much evidence it had."""

    container = _container(request)
    rows = await container.repo.list_edge_shadows(limit=limit)
    return {"items": [ser.edge_shadow(row) for row in rows]}


@router.get("/execution-telemetry")
async def execution_telemetry(
    request: Request,
    days: int = Query(default=7, ge=1, le=90),
    actor: str = Depends(require_operator),
) -> dict[str, object]:
    """How long each pipeline stage took, and how long the venue call took.

    Read-only, and derived from persisted timestamps: it reports the
    distribution and names the slowest stage rather than proposing a timeout.
    """

    container = _container(request)
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    signals = await container.repo.list_signals_with_events(since=start, until=end)
    latencies = await container.repo.list_execution_latencies()
    return ExecutionTelemetry(signals=signals, venue_latencies_ms=latencies).report()


@router.get("/positions")
async def list_positions(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    actor: str = Depends(require_operator),
) -> dict[str, object]:
    container = _container(request)
    open_only = (status or "").upper() == "OPEN"
    rows = await container.repo.list_positions(limit=limit, open_only=open_only)
    marks = await container.repo.latest_market_marks()
    return {
        "items": [ser.position(row, marks.get(row.symbol)) for row in rows]
    }


@router.get("/pnl")
async def pnl(
    request: Request,
    limit: int = Query(default=200, ge=1, le=1000),
    actor: str = Depends(require_operator),
) -> dict[str, object]:
    container = _container(request)
    rows = await container.repo.list_pnl_snapshots(limit=limit)
    account = await container.repo.get_account(container.account_id)
    return {
        "items": [ser.pnl_snapshot(row) for row in rows],
        "account": (
            {
                "realized_pnl_usdt": ser._money(account.realized_pnl_usdt),
                "daily_realized_pnl_usdt": ser._money(account.daily_realized_pnl_usdt),
                "starting_equity_usdt": ser._money(account.starting_equity_usdt),
                "peak_equity_usdt": ser._money(account.peak_equity_usdt),
            }
            if account is not None
            else None
        ),
    }


@router.get("/audit")
async def list_audit(
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    event_type: str | None = Query(default=None),
    actor: str = Depends(require_operator),
) -> dict[str, object]:
    container = _container(request)
    rows = await container.repo.list_audit(limit=limit, event_type=event_type)
    return {"items": [ser.audit_event(row) for row in rows]}


@router.get("/audit/verify")
async def verify_audit(
    request: Request, actor: str = Depends(require_operator)
) -> dict[str, object]:
    """Re-walk the hash chain and report the first inconsistency, if any."""

    container = _container(request)
    return await container.audit.verify()
