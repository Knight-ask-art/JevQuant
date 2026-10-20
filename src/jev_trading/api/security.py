"""Authentication dependencies for the control plane.

Two identities exist:

* **operator** - the human/UI session, authenticated with ``X-API-Key``.
* **execution gateway** - the component allowed to dispatch approved intents,
  authenticated with a separate ``X-Internal-Key``.

Secrets are compared in constant time and never echoed back in a response.
"""

from __future__ import annotations

import secrets

from fastapi import Request

from ..config import Settings
from ..domain.enums import ErrorCode
from ..domain.errors import AppError

ADMIN_HEADER = "X-API-Key"
INTERNAL_HEADER = "X-Internal-Key"
ACTOR_HEADER = "X-Actor"


def get_settings(request: Request) -> Settings:
    return request.app.state.container.settings


def _matches(candidate: str | None, expected: str) -> bool:
    if not candidate:
        return False
    return secrets.compare_digest(candidate, expected)


def _actor(request: Request, default: str) -> str:
    actor = request.headers.get(ACTOR_HEADER, "").strip()
    return actor[:64] if actor else default


async def require_operator(request: Request) -> str:
    """Authenticate an operator (UI/CLI) request."""

    settings = get_settings(request)
    if settings.auth_disabled:
        return _actor(request, "operator:local")
    expected = settings.admin_api_key.get_secret_value()
    if not _matches(request.headers.get(ADMIN_HEADER), expected):
        raise AppError(ErrorCode.UNAUTHORIZED, "Missing or invalid API key.")
    return _actor(request, "operator:api")


async def require_execution_identity(request: Request) -> str:
    """Authenticate the internal execution gateway."""

    settings = get_settings(request)
    expected = settings.internal_execution_key.get_secret_value()
    if not _matches(request.headers.get(INTERNAL_HEADER), expected):
        raise AppError(
            ErrorCode.UNAUTHORIZED, "Missing or invalid internal execution key."
        )
    return _actor(request, "execution:gateway")
