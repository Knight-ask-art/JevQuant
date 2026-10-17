"""Health, readiness, system status and the local demo cycle."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..domain.enums import ErrorCode, Mode
from ..domain.errors import AppError
from .security import require_operator

router = APIRouter(tags=["system"])


def _container(request: Request):
    return request.app.state.container


@router.get("/healthz", include_in_schema=False)
async def healthz(request: Request) -> dict[str, object]:
    """Liveness: the process is up and the container exists."""

    container = _container(request)
    return {
        "status": "ok",
        "service": container.settings.service_name,
        "started": container.orchestrator is not None,
    }


@router.get("/readyz", include_in_schema=False)
async def readyz(request: Request) -> dict[str, object]:
    """Readiness: every dependency needed to run a cycle is reachable."""

    container = _container(request)
    dependencies = await container.dependencies()
    required = ("database", "market")
    degraded = [
        name
        for name in required
        if not bool(dependencies.get(name, {}).get("healthy"))
    ]
    return {
        "status": "ready" if not degraded else "degraded",
        "degraded": degraded,
        "dependencies": dependencies,
    }


@router.get("/api/v1/system/status")
async def system_status(
    request: Request, actor: str = Depends(require_operator)
) -> dict[str, object]:
    return await _container(request).status()


@router.get("/api/v1/system/config")
async def system_config(
    request: Request, actor: str = Depends(require_operator)
) -> dict[str, object]:
    """Redacted configuration. Secret values are never included."""

    container = _container(request)
    return {
        "config": container.settings.redacted_summary(),
        "execution_backend": container.settings.execution_backend,
        "data_source": container.market.source,
    }


@router.post("/api/v1/demo/cycle")
async def run_demo_cycle(
    request: Request,
    refresh_policy: bool = False,
    actor: str = Depends(require_operator),
) -> dict[str, object]:
    """Run one trading cycle immediately (local demo / operations tooling)."""

    container = _container(request)
    mode = await container.modes.current_mode()
    if mode is Mode.LIVE:
        raise AppError(
            ErrorCode.FORBIDDEN,
            "Manual cycles are disabled in live mode. Use the configured "
            "auto-cycle scheduler instead.",
        )
    result = await container.run_cycle(refresh_policy=refresh_policy)
    return result.model_dump(mode="json")
