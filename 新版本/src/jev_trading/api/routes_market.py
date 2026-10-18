"""Symbol universe and price charts.

The universe the operator picks is a *hard* limit, stored in ``system_state``
and applied to the live settings and the policy compiler before anything is
generated: a symbol that is not on the list cannot reach the LLM, Jev, risk
or execution, whatever the model proposes. The compiled policy may still be
narrower than this list; it is never wider.

Charts read public klines. When the market provider cannot serve them (for
example a restricted location), the endpoint says so instead of drawing a
line from synthetic data.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..domain.enums import ErrorCode
from ..domain.errors import AppError
from ..services.klines import (
    INTERVALS,
    CandleSeries,
    clamp_limit,
    to_series,
    validate_interval,
)
from .security import require_operator

router = APIRouter(prefix="/api/v1/market", tags=["market"])


def _container(request: Request):
    return request.app.state.container


class UniverseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[str] = Field(min_length=1, max_length=10)

    @field_validator("symbols")
    @classmethod
    def _normalize(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for symbol in value:
            upper = symbol.strip().upper()
            if not upper.isalnum() or not upper.endswith("USDT") or not (5 <= len(upper) <= 20):
                raise ValueError(f"{symbol!r} is not a USDT spot symbol")
            if upper not in normalized:
                normalized.append(upper)
        if not normalized:
            raise ValueError("at least one symbol is required")
        return normalized


@router.get("/universe")
async def universe(
    request: Request, actor: str = Depends(require_operator)
) -> dict[str, Any]:
    container = _container(request)
    marks = await container.repo.latest_market_marks()
    policy = await container.policy_service.get_active(
        (await container.modes.current_mode()).value
    )
    # With real market data, offer the liquid USDT pairs the venue lists so
    # the operator can add one without editing any file. The synthetic feed
    # can only serve its own symbols.
    candidates: list[dict[str, Any]] = []
    if not container.settings.synthetic_market:
        try:
            candidates = await container.market.spot_candidates()
        except AppError:
            candidates = []
    return {
        "symbols": container.symbol_universe(),
        "configured": container.settings.allowed_symbols,
        "defaults": container.default_universe,
        "candidates": candidates,
        "active_universe": sorted(item.symbol for item in policy.universe) if policy else [],
        "marks": {symbol: str(price) for symbol, price in marks.items()},
        "source": container.market.source,
        "synthetic": container.settings.synthetic_market,
    }


@router.put("/universe")
async def set_universe(
    request: Request,
    payload: UniverseRequest,
    actor: str = Depends(require_operator),
) -> dict[str, Any]:
    container = _container(request)
    # Fail closed: a symbol whose exchange rules cannot be read never enters
    # the list, because nothing downstream could size or exit it safely.
    rejected: dict[str, str] = {}
    for symbol in payload.symbols:
        try:
            filters = await container.market.filters(symbol, force=True)
            filters.require_tradable()
        except AppError as exc:
            rejected[symbol] = exc.message
        except ValueError as exc:
            rejected[symbol] = str(exc)
    accepted = [symbol for symbol in payload.symbols if symbol not in rejected]
    if not accepted:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "None of the selected symbols has readable exchange rules.",
            details={"rejected": rejected},
        )

    async with container.settings_lock():
        await container.apply_symbol_universe(accepted, actor=actor)
    return {
        "symbols": accepted,
        "rejected": rejected,
        "configured": container.settings.allowed_symbols,
        "server_limits": {
            "allowed_symbols": list(container.settings.server_limits.allowed_symbols),
        },
    }


@router.get("/candles", response_model=CandleSeries)
async def candles(
    request: Request,
    symbol: str = Query(min_length=5, max_length=20),
    interval: str = Query(default="15m"),
    limit: int = Query(default=120, ge=20, le=500),
    actor: str = Depends(require_operator),
) -> CandleSeries:
    container = _container(request)
    try:
        validate_interval(interval)
    except ValueError as exc:
        raise AppError(ErrorCode.VALIDATION_ERROR, str(exc)) from exc
    normalized = symbol.strip().upper()
    series = await container.market.klines(
        normalized, interval=interval, limit=clamp_limit(limit)
    )
    return to_series(
        symbol=normalized,
        interval=interval,
        source=container.market.source,
        synthetic=container.settings.synthetic_market,
        candles=series,
    )


@router.get("/intervals")
async def intervals(actor: str = Depends(require_operator)) -> dict[str, list[str]]:
    return {"intervals": list(INTERVALS)}
