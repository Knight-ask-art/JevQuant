"""Binance Spot public market-data adapter.

No credentials are used for market data. HTTP 451 (restricted location)
is surfaced as a dependency failure; the system never circumvents it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Sequence

import httpx

from ...domain.errors import (
    DependencyBadResponseError,
    DependencyUnavailableError,
    ProviderRateLimitedError,
)
from ...domain.market import Candle, MarketSnapshot, SymbolFilters
from ...services.features import build_features
from ...services.klines import INTERVALS


class BinanceMarketDataProvider:
    source = "binance"

    def __init__(self, base_url: str = "https://api.binance.com", *, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)
        self._exchange_info: dict[str, Any] | None = None
        self._exchange_info_at: datetime | None = None
        self._semaphore = asyncio.Semaphore(4)
        self.last_used_weight: int | None = None
        self.ban_until: datetime | None = None

    async def _request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        now = datetime.now(UTC)
        if self.ban_until is not None and now < self.ban_until:
            raise DependencyUnavailableError(
                "Binance IP ban is active; no requests will be sent.",
                details={"ban_until": self.ban_until.isoformat()},
            )
        try:
            response = await self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise DependencyUnavailableError(
                "Binance market-data request failed.",
                details={"reason": type(exc).__name__},
            ) from exc

        used_weight = response.headers.get("X-MBX-USED-WEIGHT-1M")
        if used_weight is not None:
            try:
                self.last_used_weight = int(used_weight)
            except ValueError:
                pass

        if response.status_code == 451:
            raise DependencyUnavailableError(
                "Binance returned HTTP 451 (restricted location).",
                details={"http_status": 451, "policy": "stop, do not circumvent"},
            )
        if response.status_code == 418:
            retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
            self.ban_until = now + timedelta(seconds=retry_after or 120)
            raise DependencyUnavailableError(
                "Binance returned HTTP 418 (IP ban).",
                details={"retry_after_s": retry_after or 120},
            )
        if response.status_code == 429:
            retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
            raise ProviderRateLimitedError(
                "Binance rate limit reached.",
                retry_after_ms=int((retry_after or 1) * 1000),
            )
        if response.status_code >= 500:
            raise DependencyUnavailableError(
                "Binance returned a server error.",
                details={"http_status": response.status_code},
            )
        if response.status_code >= 400:
            raise DependencyBadResponseError(
                "Binance rejected the market-data request.",
                details={"http_status": response.status_code},
            )
        try:
            return response.json()
        except ValueError as exc:
            raise DependencyBadResponseError("Binance returned non-JSON data.") from exc

    async def exchange_info(self, *, force: bool = False) -> dict[str, Any]:
        now = datetime.now(UTC)
        if (
            not force
            and self._exchange_info is not None
            and self._exchange_info_at is not None
            and now - self._exchange_info_at < timedelta(minutes=10)
        ):
            return self._exchange_info
        payload = await self._request("/api/v3/exchangeInfo")
        if not isinstance(payload, dict) or "symbols" not in payload:
            raise DependencyBadResponseError("exchangeInfo response is missing symbols.")
        self._exchange_info = payload
        self._exchange_info_at = now
        return payload

    async def filters(self, symbol: str) -> SymbolFilters:
        payload = await self.exchange_info()
        return SymbolFilters.from_binance_exchange_info(payload, symbol)

    async def _snapshot(self, symbol: str, now: datetime) -> MarketSnapshot:
        async with self._semaphore:
            book, depth, klines = await asyncio.gather(
                self._request("/api/v3/ticker/bookTicker", {"symbol": symbol}),
                self._request("/api/v3/depth", {"symbol": symbol, "limit": 10}),
                self._request(
                    "/api/v3/klines",
                    {"symbol": symbol, "interval": "1m", "limit": 240},
                ),
            )
        if not isinstance(book, dict) or "bidPrice" not in book:
            raise DependencyBadResponseError(f"bookTicker malformed for {symbol}")
        bid = Decimal(str(book["bidPrice"]))
        ask = Decimal(str(book["askPrice"]))
        if bid <= 0 or ask <= 0 or ask < bid:
            raise DependencyBadResponseError(f"bookTicker crossed for {symbol}")

        candle_objects: list[Candle] = []
        for row in klines:
            close_time = datetime.fromtimestamp(int(row[6]) / 1000, tz=UTC)
            if close_time > now:
                continue
            candle_objects.append(
                Candle(
                    open_time=datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC),
                    close_time=close_time,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
            )
        if len(candle_objects) < 30:
            raise DependencyBadResponseError(f"not enough closed candles for {symbol}")

        bid_volume = sum(float(level[1]) for level in depth.get("bids", []))
        ask_volume = sum(float(level[1]) for level in depth.get("asks", []))
        total = bid_volume + ask_volume
        imbalance = (bid_volume - ask_volume) / total if total > 0 else 0.0

        features = build_features(candle_objects, book_imbalance=imbalance)
        return MarketSnapshot(
            symbol=symbol,
            as_of=now,
            source=self.source,
            bid=bid,
            ask=ask,
            candle_close_at=candle_objects[-1].close_time,
            features=features,
            is_synthetic=False,
        )

    async def snapshots(
        self, symbols: Sequence[str], now: datetime | None = None
    ) -> list[MarketSnapshot]:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        results = await asyncio.gather(
            *(self._snapshot(symbol, now) for symbol in symbols), return_exceptions=True
        )
        snapshots: list[MarketSnapshot] = []
        errors: list[BaseException] = []
        for result in results:
            if isinstance(result, BaseException):
                errors.append(result)
            else:
                snapshots.append(result)
        if errors and not snapshots:
            raise errors[0]
        return snapshots

    async def spot_candidates(self, limit: int = 40) -> list[dict[str, Any]]:
        """Trading USDT spot pairs by 24h quote volume, for the universe picker."""

        payload = await self._request("/api/v3/ticker/24hr")
        if not isinstance(payload, list):
            raise DependencyBadResponseError("24h ticker response was not a list.")
        info = await self.exchange_info()
        permitted = {
            item.get("symbol")
            for item in info.get("symbols", [])
            if item.get("status") == "TRADING" and item.get("quoteAsset") == "USDT"
        }
        ranked = sorted(
            (
                item
                for item in payload
                if isinstance(item, dict) and item.get("symbol") in permitted
            ),
            key=lambda item: float(item.get("quoteVolume") or 0),
            reverse=True,
        )
        return [
            {
                "symbol": item["symbol"],
                "base": item["symbol"][: -len("USDT")],
                "quote_volume_usdt": item.get("quoteVolume"),
                "price_change_pct": item.get("priceChangePercent"),
                "last_price": item.get("lastPrice"),
            }
            for item in ranked[:limit]
        ]

    async def klines(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 120,
        warmup: int = 0,
        start: datetime | None = None,
    ) -> list[Candle]:
        """Closed candles for charting. The last, still-forming bar is dropped.

        ``warmup`` extra bars are prepended so a caller can compute an
        indicator over history and then show only the last ``limit`` bars.
        ``start`` asks for the first bars at or after that instant, which is
        what an offline replay needs to walk a past window.
        """

        if interval not in INTERVALS:
            raise DependencyBadResponseError(f"unsupported kline interval {interval!r}")
        wanted = int(limit) + max(0, int(warmup))
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "limit": min(wanted + 1, 1000),
        }
        if start is not None:
            params["startTime"] = int(start.timestamp() * 1000)
        rows = await self._request("/api/v3/klines", params)
        if not isinstance(rows, list):
            raise DependencyBadResponseError("klines response was not a list.")
        now = datetime.now(UTC)
        candles: list[Candle] = []
        for row in rows:
            try:
                close_time = datetime.fromtimestamp(int(row[6]) / 1000, tz=UTC)
                if close_time > now:
                    continue
                candles.append(
                    Candle(
                        open_time=datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC),
                        close_time=close_time,
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                    )
                )
            except (IndexError, TypeError, ValueError) as exc:
                raise DependencyBadResponseError(f"klines row malformed for {symbol}") from exc
        return candles[-wanted:]

    async def health(self) -> dict[str, object]:
        try:
            await self._request("/api/v3/ping")
            return {
                "healthy": True,
                "source": self.source,
                "used_weight_1m": self.last_used_weight,
            }
        except Exception as exc:  # noqa: BLE001 - health must never raise
            return {
                "healthy": False,
                "source": self.source,
                "error": type(exc).__name__,
                "detail": str(exc)[:200],
            }

    async def close(self) -> None:
        await self._client.aclose()


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
