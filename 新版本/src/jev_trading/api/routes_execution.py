"""Execution gateway endpoints (internal identity)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from ..domain.enums import ErrorCode
from ..domain.errors import AppError
from .security import require_execution_identity, require_operator

router = APIRouter(prefix="/api/v1/execution", tags=["execution"])


def _container(request: Request):
    return request.app.state.container


class DispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent_id: str


@router.post("/intents")
async def dispatch_intent(
    request: Request,
    payload: DispatchRequest,
    actor: str = Depends(require_execution_identity),
) -> dict[str, object]:
    """Submit an approved intent to the exchange adapter for the active mode.

    Requires the internal execution key. The intent must exist, be APPROVED
    with a persisted risk decision, be unexpired, and match the active mode.
    """

    container = _container(request)
    if container.orchestrator is None:
        raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Runtime is not started.")
    return await container.orchestrator.dispatch_approved_intent(payload.intent_id)


@router.get("/health")
async def execution_health(
    request: Request, actor: str = Depends(require_operator)
) -> dict[str, object]:
    container = _container(request)
    mode = await container.modes.current_mode()
    adapter = container.execution_for_mode.get(mode)
    health = await adapter.health() if adapter is not None else {"healthy": False}
    return {
        "mode": mode.value,
        "backend": container.settings.execution_backend,
        "adapter": type(adapter).__name__ if adapter is not None else None,
        "health": health,
    }


@router.post("/reconcile")
async def reconcile(
    request: Request, actor: str = Depends(require_execution_identity)
) -> dict[str, object]:
    """Ask the active adapter to re-check orders whose state is unknown."""

    container = _container(request)
    mode = await container.modes.current_mode()
    adapter = container.execution_for_mode.get(mode)
    if adapter is None:
        raise AppError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            f"No execution adapter is configured for mode {mode.value}.",
        )
    result = await adapter.reconcile(container.account_id)
    await container.audit.record(
        "execution.reconciled",
        "system",
        None,
        result.model_dump(mode="json") if hasattr(result, "model_dump") else {},
    )
    return result.model_dump(mode="json")
