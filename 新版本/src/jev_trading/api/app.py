"""FastAPI control plane.

The app owns a single ``AppContainer`` for its lifetime. Startup builds the
database schema, initializes the mode manager (LIVE is always reset to PAPER
on restart), creates the paper account, wires the orchestrator and records a
``system.started`` audit event.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..config import Settings, get_settings
from ..domain.errors import AppError
from ..services.container import AppContainer
from .routes_activity import router as activity_router
from .routes_backtest import router as backtest_router
from .routes_connections import router as connections_router
from .routes_execution import router as execution_router
from .routes_mode import router as mode_router
from .routes_settings import router as settings_router
from .routes_strategy import router as strategy_router
from .routes_system import router as system_router
from .ws import router as ws_router

logger = logging.getLogger("jev_trading.api")


def _error_response(status_code: int, code: str, message: str, trace_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "retryable": False,
                "retry_after_ms": None,
                "trace_id": trace_id,
                "details": {},
            }
        },
        headers={"X-Trace-Id": trace_id},
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    container = AppContainer(resolved)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await container.start()
        logger.info(
            "control plane started mode=%s backend=%s synthetic=%s",
            (await container.modes.current_mode()).value,
            resolved.execution_backend,
            resolved.synthetic_market,
        )
        try:
            yield
        finally:
            await container.close()

    app = FastAPI(
        title="Jev Trading Control Plane",
        version="0.1.0",
        description=(
            "LLM strategy authoring -> deterministic compiler -> Jev evaluation "
            "-> independent risk engine -> execution gateway."
        ),
        lifespan=lifespan,
    )
    app.state.container = container
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        trace_id = exc.trace_id or str(uuid4())
        if exc.trace_id is None:
            exc.trace_id = trace_id
        logger.warning(
            "app error trace=%s code=%s status=%s",
            trace_id,
            exc.code.value,
            exc.http_status,
        )
        return JSONResponse(
            status_code=exc.http_status,
            content=exc.to_contract(),
            headers={"X-Trace-Id": trace_id},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        trace_id = str(uuid4())
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Request body or parameters failed validation.",
                    "retryable": False,
                    "retry_after_ms": None,
                    "trace_id": trace_id,
                    "details": {"errors": jsonable_encoder(exc.errors()[:10])},
                }
            },
            headers={"X-Trace-Id": trace_id},
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        trace_id = str(uuid4())
        logger.exception("unhandled error trace=%s", trace_id)
        return _error_response(
            500, "INTERNAL_ERROR", "An internal error occurred.", trace_id
        )

    app.include_router(system_router)
    app.include_router(mode_router)
    app.include_router(settings_router)
    app.include_router(strategy_router)
    app.include_router(activity_router)
    app.include_router(execution_router)
    app.include_router(backtest_router)
    app.include_router(connections_router)
    app.include_router(ws_router)
    return app


app = create_app()
