"""Operator authentication: API key for CLI, HttpOnly session for browser.

The browser never persists the administrator key or sends it in a WebSocket URL.
Cookie sessions are HMAC authenticated and expire after eight hours; unsafe
cookie-authenticated requests require a per-session CSRF header.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from urllib.parse import urlparse

from fastapi import Request, WebSocket

from ..config import Settings
from ..domain.enums import ErrorCode, Mode
from ..domain.errors import AppError

ADMIN_HEADER = "X-API-Key"
INTERNAL_HEADER = "X-Internal-Key"
ACTOR_HEADER = "X-Actor"
SESSION_COOKIE = "jev_operator_session"
SESSION_TTL_SECONDS = 8 * 3600
CSRF_HEADER = "X-CSRF-Token"


def get_settings(request: Request) -> Settings:
    return request.app.state.container.settings


def _matches(candidate: str | None, expected: str) -> bool:
    return bool(candidate) and secrets.compare_digest(candidate, expected)


def _actor(request: Request, default: str) -> str:
    actor = request.headers.get(ACTOR_HEADER, "").strip()
    return actor[:64] if actor else default


def local_auth_off_allowed(settings: Settings, mode: Mode) -> bool:
    """When may the login legitimately stay off?

    Two ways, and neither is a silent bypass:

    * a local, synthetic, development account (the original guard), or
    * a host explicitly declared ``auth_tunnel_only``: auth is off because nothing
      but the SSH tunnel can reach the port. A host that can trade live can
      never claim that, so the declaration cannot become a backdoor.
    """
    if settings.enable_live_trading or mode is Mode.LIVE:
        return False
    if (
        settings.app_env == "dev"
        and settings.synthetic_market
        and mode in (Mode.PAPER, Mode.SHADOW)
    ):
        return True
    return settings.auth_tunnel_only


async def auth_required(container) -> bool:
    mode = await container.modes.current_mode()
    return not (container.settings.auth_disabled and local_auth_off_allowed(container.settings, mode))


def _signing_key(settings: Settings) -> bytes:
    return hashlib.sha256(
        ("jev-operator-session-v1:" + settings.admin_api_key.get_secret_value()).encode("utf-8")
    ).digest()


def new_session(settings: Settings) -> str:
    expiration = str(int(time.time()) + SESSION_TTL_SECONDS)
    nonce = secrets.token_urlsafe(24)
    message = f"{expiration}.{nonce}"
    mac = hmac.new(_signing_key(settings), message.encode(), hashlib.sha256).hexdigest()
    return f"{message}.{mac}"


def is_valid_session(token: str | None, settings: Settings) -> bool:
    if not token or len(token) > 512:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    expiration, nonce, signature = parts
    try:
        expires_at = int(expiration)
    except ValueError:
        return False
    if not nonce or expires_at <= time.time() or expires_at > time.time() + SESSION_TTL_SECONDS:
        return False
    message = f"{expiration}.{nonce}"
    expected = hmac.new(_signing_key(settings), message.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def csrf_for_session(token: str, settings: Settings) -> str:
    return hmac.new(_signing_key(settings), ("csrf:" + token).encode(), hashlib.sha256).hexdigest()


async def require_operator(request: Request) -> str:
    settings = get_settings(request)
    if not await auth_required(request.app.state.container):
        return _actor(request, "operator:local")
    if _matches(request.headers.get(ADMIN_HEADER), settings.admin_api_key.get_secret_value()):
        return _actor(request, "operator:api")
    token = request.cookies.get(SESSION_COOKIE)
    if not is_valid_session(token, settings):
        raise AppError(ErrorCode.UNAUTHORIZED, "Please sign in to the operator console.")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        provided = request.headers.get(CSRF_HEADER, "")
        if not _matches(provided, csrf_for_session(token, settings)):
            raise AppError(ErrorCode.FORBIDDEN, "Missing or invalid CSRF token.")
    return _actor(request, "operator:session")


async def websocket_authorized(websocket: WebSocket) -> bool:
    container = websocket.app.state.container
    if not await auth_required(container):
        return True
    origin = websocket.headers.get("origin")
    if origin and urlparse(origin).netloc != websocket.headers.get("host"):
        return False
    return is_valid_session(websocket.cookies.get(SESSION_COOKIE), container.settings)


async def require_execution_identity(request: Request) -> str:
    settings = get_settings(request)
    expected = settings.internal_execution_key.get_secret_value()
    if not _matches(request.headers.get(INTERNAL_HEADER), expected):
        raise AppError(ErrorCode.UNAUTHORIZED, "Missing or invalid internal execution key.")
    return _actor(request, "execution:gateway")
