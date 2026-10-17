"""Mode control, the live arming handshake and the kill switch."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from ..domain.enums import Mode
from .security import require_operator

router = APIRouter(prefix="/api/v1", tags=["mode"])


def _container(request: Request):
    return request.app.state.container


class SetModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["paper", "testnet", "shadow", "live"]


class ArmLiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arming_token: str = Field(min_length=16)
    confirmation: str = Field(min_length=1)
    totp_code: str = Field(min_length=6, max_length=10)


class ReasonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=200)


@router.post("/mode/preflight-live")
async def preflight_live(
    request: Request, actor: str = Depends(require_operator)
) -> dict[str, object]:
    """Inspect every precondition for live trading.

    A passing preflight returns a single-use arming token. It does not arm
    anything: the token must be redeemed with a confirmation phrase and a
    valid TOTP code, and it expires quickly.
    """

    return await _container(request).modes.preflight_live(actor=actor)


@router.post("/mode/arm-live")
async def arm_live(
    request: Request,
    payload: ArmLiveRequest,
    actor: str = Depends(require_operator),
) -> dict[str, object]:
    return await _container(request).modes.arm_live(
        arming_token=payload.arming_token,
        confirmation=payload.confirmation,
        totp_code=payload.totp_code,
        actor=actor,
    )


@router.post("/mode/disarm")
async def disarm(
    request: Request,
    payload: ReasonRequest,
    actor: str = Depends(require_operator),
) -> dict[str, object]:
    """Disarm live trading. Existing positions keep their deterministic exits."""

    return await _container(request).modes.disarm(reason=payload.reason, actor=actor)


@router.post("/mode/set")
async def set_mode(
    request: Request,
    payload: SetModeRequest,
    actor: str = Depends(require_operator),
) -> dict[str, object]:
    return await _container(request).modes.set_mode(
        Mode(payload.mode), actor=actor
    )


@router.post("/kill-switch")
async def kill_switch(
    request: Request,
    payload: ReasonRequest,
    actor: str = Depends(require_operator),
) -> dict[str, object]:
    """Block all new entries. Open positions are still managed to their exit."""

    return await _container(request).modes.kill(reason=payload.reason, actor=actor)


@router.post("/kill-switch/reset")
async def reset_kill_switch(
    request: Request, actor: str = Depends(require_operator)
) -> dict[str, object]:
    return await _container(request).modes.reset_kill_switch(actor=actor)
