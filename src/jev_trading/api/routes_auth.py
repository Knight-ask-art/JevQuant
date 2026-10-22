"""Interactive, configurable browser authentication.

Only local synthetic development can disable login. Production, testnet and
live modes always require authentication, regardless of the UI switch.
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ..domain.enums import ErrorCode
from ..domain.errors import AppError
from .security import (
    SESSION_COOKIE, SESSION_TTL_SECONDS, auth_required, csrf_for_session,
    is_valid_session, local_auth_off_allowed, new_session, require_operator,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: str = Field(min_length=1, max_length=4096)


class AuthSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    admin_key: str | None = Field(default=None, max_length=4096)


def _set_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        key=SESSION_COOKIE, value=token, max_age=SESSION_TTL_SECONDS,
        httponly=True, secure=request.app.state.container.settings.is_production
        or request.url.scheme == "https", samesite="strict", path="/",
    )


@router.get("/status")
async def status(request: Request) -> dict[str, object]:
    container = request.app.state.container
    settings = container.settings
    required = await auth_required(container)
    token = request.cookies.get(SESSION_COOKIE)
    valid = is_valid_session(token, settings)
    mode = await container.modes.current_mode()
    result: dict[str, object] = {
        "enabled": required,
        "authenticated": not required or valid,
        "can_disable": local_auth_off_allowed(settings, mode),
    }
    if required and valid and token:
        result["csrf_token"] = csrf_for_session(token, settings)
    return result


@router.post("/login")
async def login(request: Request, response: Response, body: LoginRequest) -> dict[str, str]:
    settings = request.app.state.container.settings
    actual = settings.admin_api_key.get_secret_value()
    if not secrets.compare_digest(body.api_key, actual) or actual == "change-me-in-production":
        raise AppError(ErrorCode.UNAUTHORIZED, "Invalid operator credential.")
    token = new_session(settings)
    _set_cookie(response, token, request)
    return {"csrf_token": csrf_for_session(token, settings)}


@router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"logged_out": True}


@router.put("/settings")
async def update_auth(
    request: Request,
    body: AuthSettingsRequest,
    actor: str = Depends(require_operator),
) -> dict[str, object]:
    container = request.app.state.container
    settings = container.settings
    mode = await container.modes.current_mode()
    if not body.enabled and not local_auth_off_allowed(settings, mode):
        raise AppError(ErrorCode.FORBIDDEN, "Authentication cannot be disabled in this environment.")
    if body.enabled:
        key = settings.admin_api_key.get_secret_value()
        if key == "change-me-in-production" or not body.admin_key or not secrets.compare_digest(body.admin_key, key):
            raise AppError(ErrorCode.FORBIDDEN, "A configured administrator key is required to enable login.")
    # Settings is the request-time source of truth; persist only after policy checks.
    settings.auth_disabled = not body.enabled
    await container.repo.set_state("auth_disabled_override", settings.auth_disabled)
    await container.audit.record(
        "security.auth_toggled", "settings", None,
        {"enabled": body.enabled, "actor": actor, "mode": mode.value},
    )
    return {"enabled": body.enabled}
