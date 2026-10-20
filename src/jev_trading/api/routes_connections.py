"""Provider connectivity checks.

Every response is a health summary. Credentials, request headers and raw
provider payloads never leave this module.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..domain.errors import AppError
from .security import require_operator

router = APIRouter(prefix="/api/v1/connections", tags=["connections"])


def _container(request: Request):
    return request.app.state.container


@router.post("/opencode/test")
async def test_opencode(
    request: Request, actor: str = Depends(require_operator)
) -> dict[str, object]:
    container = _container(request)
    return {
        "configured": container.settings.opencode_console_enabled,
        "simulated": (
            getattr(container.policy_generator, "provider", "")
            not in {"opencode_console", "opencode_go"}
        ),
        "health": await container.policy_generator.health(),
    }


@router.get("/opencode/models")
async def opencode_models(
    request: Request, actor: str = Depends(require_operator)
) -> dict[str, object]:
    container = _container(request)
    lister = getattr(container.policy_generator, "list_models", None)
    if not callable(lister):
        return {
            "models": [],
            "simulated": True,
            "note": "The simulated policy generator exposes no model catalogue.",
        }
    try:
        return {"models": await lister(), "simulated": False}
    except AppError as exc:
        return {
            "models": [],
            "simulated": False,
            "error": exc.to_contract(),
        }


@router.post("/binance/test")
async def test_binance(
    request: Request, actor: str = Depends(require_operator)
) -> dict[str, object]:
    """Inspect Binance key restrictions via ``/sapi/v1/account/apiRestrictions``.

    With no credentials this returns ``configured: false`` without making a
    network call. In a restricted location Binance may answer HTTP 451; that
    is reported as-is and is never worked around.
    """

    container = _container(request)
    settings = container.settings
    if not (settings.binance_api_key and settings.binance_api_secret):
        return {
            "configured": False,
            "skipped": True,
            "reason": "Binance API credentials are not configured.",
        }
    try:
        result = await container.binance_account.preflight(
            require_ip_restrict=settings.require_ip_restrict,
            require_withdrawals_disabled=settings.require_withdrawals_disabled,
        )
    except AppError as exc:
        return {"configured": True, "passed": False, "error": exc.to_contract()}
    return {
        "configured": True,
        "passed": result["passed"],
        "checks": result["checks"],
    }


@router.post("/jev/test")
async def test_jev(
    request: Request, actor: str = Depends(require_operator)
) -> dict[str, object]:
    container = _container(request)
    return {
        "configured": container.settings.jev_enabled,
        "simulated": getattr(container.jev, "provider", "") == "simulated",
        "health": await container.jev.health(),
    }


@router.get("/hummingbot/health")
async def hummingbot_health(
    request: Request, actor: str = Depends(require_operator)
) -> dict[str, object]:
    container = _container(request)
    return {
        "configured": bool(
            container.settings.hummingbot_base_url
            and container.settings.hummingbot_api_key
        ),
        "health": await container.hummingbot.health(),
    }
