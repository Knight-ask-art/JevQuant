"""Strategy policy endpoints: generation, inspection and comparison.

``/agent`` exposes the strategy agent's journal: what it read, which tools it
called, and how the resulting policy performed. Generation is serialized with
trading cycles so a manual run never races the auto-loop for the same account.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict

from ..domain.enums import ErrorCode
from ..domain.errors import AppError
from ..domain.money import canonical_str
from ..services.agent_tools import AgentToolbox
from ..services.policy_service import policy_display_status
from . import serializers as ser
from .security import require_operator

router = APIRouter(prefix="/api/v1/strategy", tags=["strategy"])


def _container(request: Request):
    return request.app.state.container


class GeneratePolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: Literal["trade_runtime", "strategy_authoring"] = "trade_runtime"
    persist_market: bool = True


@router.post("/policies/generate")
async def generate_policy(
    request: Request,
    payload: GeneratePolicyRequest | None = None,
    actor: str = Depends(require_operator),
) -> dict[str, object]:
    """Ask the strategy agent for a bounded policy and compile it server-side.

    The returned policy is always the *effective* one: the compiler narrows
    whatever the model produced to the server envelope. Raw model output is
    kept for audit but is never what the runtime consumes.
    """

    container = _container(request)
    body = payload or GeneratePolicyRequest()
    mode = await container.modes.current_mode()
    now = datetime.now(UTC)
    symbols = sorted(container.settings.allowed_symbols)

    async with container.policy_generation_lock():
        snapshots = []
        try:
            snapshots = await container.market.snapshots(symbols, now)
        except AppError:
            snapshots = []
        if body.persist_market:
            for snapshot in snapshots:
                await container.repo.insert_market_snapshot(snapshot)

        marks = {snapshot.symbol: snapshot.mid for snapshot in snapshots}
        account = await container.repo.account_snapshot(container.account_id, marks, now)
        market_summary = {
            "universe": symbols,
            "timeframes": ["1m", "5m"],
            "as_of": now.isoformat(),
            "symbols": {
                snapshot.symbol: {
                    "snapshot_id": str(snapshot.snapshot_id),
                    "mid": float(snapshot.mid),
                    "spread_bps": float(snapshot.spread_bps),
                    **snapshot.features,
                }
                for snapshot in snapshots
            },
            "data_source": container.market.source,
            "simulated": any(snapshot.is_synthetic for snapshot in snapshots),
        }
        account_state = {
            "equity_usdt": float(account.equity_usdt),
            "available_quote_usdt": float(account.available_quote_usdt),
            "open_positions": account.open_positions,
            "daily_realized_pnl_pct": canonical_str(account.daily_realized_pnl_pct),
        }

        generator = container.policy_generator
        ensure = getattr(generator, "ensure_purpose", None)
        if callable(ensure):
            ensure(body.purpose)

        toolbox = AgentToolbox(
            repo=container.repo,
            market=container.market,
            account_id=container.account_id,
            mode=mode.value,
            symbols=container.settings.allowed_symbols,
            snapshots=snapshots,
            now=now,
            settings=container.settings,
        )
        effective, row = await container.policy_service.generate(
            generator=generator,
            mode=mode.value,
            account_state=account_state,
            market_summary=market_summary,
            purpose=body.purpose,
            toolbox=toolbox,
        )
    agent_run = None
    run = await container.repo.latest_agent_run_for_policy(str(effective.policy_id))
    if run is not None:
        agent_run = ser.agent_run(run, include_tools=True)
    return {
        "policy_id": str(effective.policy_id),
        "policy_hash": effective.policy_hash,
        "source": row.source,
        "simulated": row.is_simulated,
        "purpose": body.purpose,
        "mode_scope": [item.value for item in effective.mode_scope],
        "expires_at": effective.expires_at.isoformat(),
        "clamps": [clamp.model_dump(mode="json") for clamp in effective.clamps],
        "raw": row.raw_json,
        "effective": row.effective_json,
        "rationale": (row.raw_json or {}).get("rationale"),
        "agent": agent_run,
    }


@router.get("/agent")
async def agent_journal(
    request: Request,
    limit: int = Query(default=12, ge=1, le=100),
    actor: str = Depends(require_operator),
) -> dict[str, object]:
    """Recent strategy-agent runs. The newest run includes its tool trace."""

    return await _container(request).agent_status(limit=limit)


@router.get("/agent/{run_id}")
async def agent_run_detail(
    run_id: str, request: Request, actor: str = Depends(require_operator)
) -> dict[str, object]:
    container = _container(request)
    row = await container.repo.get_agent_run(run_id)
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, f"Agent run {run_id} was not found.")
    return ser.agent_run(row, include_tools=True)


@router.get("/policies/current")
async def current_policy(
    request: Request, actor: str = Depends(require_operator)
) -> dict[str, object]:
    container = _container(request)
    mode = await container.modes.current_mode()
    view = await container.policy_service.current(mode.value)
    row = view["policy"]
    if row is None:
        return {
            "active": False,
            "expired": False,
            "reason": view["reason"],
            "compiled": None,
            "policy": None,
        }
    effective = view["compiled"]
    return {
        "active": view["active"],
        "expired": view["expired"],
        "reason": view["reason"],
        "compiled": container.policy_service.describe(effective) if effective else None,
        "policy": {
            "id": row.id,
            "policy_id": row.policy_id,
            "status": view["status"],
            "active": view["active"],
            "expired": view["expired"],
            "source": row.source,
            "is_simulated": row.is_simulated,
            "mode_scope": row.mode_scope_json,
            "clamps": row.clamps_json,
            "policy_hash": row.policy_hash,
            "raw": row.raw_json,
            "effective": row.effective_json,
            "generated_at": row.generated_at.isoformat(),
            "expires_at": row.expires_at.isoformat(),
        },
    }


@router.get("/policies")
async def list_policies(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    actor: str = Depends(require_operator),
) -> dict[str, object]:
    container = _container(request)
    rows = await container.repo.list_policies(limit=limit)
    return {
        "items": [
            {
                "id": row.id,
                "policy_id": row.policy_id,
                "status": policy_display_status(row)[0],
                "expired": policy_display_status(row)[1],
                "source": row.source,
                "is_simulated": row.is_simulated,
                "regime": (row.effective_json or {}).get("regime", {}).get("label"),
                "policy_hash": row.policy_hash,
                "generated_at": row.generated_at.isoformat(),
                "expires_at": row.expires_at.isoformat(),
            }
            for row in rows
        ]
    }


@router.get("/policies/{policy_id}")
async def get_policy(
    policy_id: str, request: Request, actor: str = Depends(require_operator)
) -> dict[str, object]:
    container = _container(request)
    row = await container.repo.get_policy_by_policy_id(policy_id)
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, f"Policy {policy_id} was not found.")
    status, expired = policy_display_status(row)
    return {
        "id": row.id,
        "policy_id": row.policy_id,
        "status": status,
        "expired": expired,
        "source": row.source,
        "is_simulated": row.is_simulated,
        "mode_scope": row.mode_scope_json,
        "clamps": row.clamps_json,
        "policy_hash": row.policy_hash,
        "raw": row.raw_json,
        "effective": row.effective_json,
        "generated_at": row.generated_at.isoformat(),
        "expires_at": row.expires_at.isoformat(),
    }
