"""In-process fan-out plus optional Redis Streams publishing.

A Redis outage never silently changes trading decisions; the broker exposes
its health so the system can fail closed on new entries.  Local subscribers
(WebSocket clients) always receive the event in-process.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, AsyncIterator

STREAMS = {
    "market.features": "market.features",
    "policy.updated": "policy.updated",
    "signal.candidates": "signal.candidates",
    "trade.intents.approved": "trade.intents.approved",
    "execution.events": "execution.events",
    "audit.ingest": "audit.ingest",
    "system.events": "system.events",
}


class EventBroker:
    def __init__(self, redis_url: str | None = None, *, max_queue: int = 500) -> None:
        self.redis_url = redis_url
        self.max_queue = max_queue
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._redis: Any | None = None
        self.redis_healthy = True
        self.redis_configured = bool(redis_url)

    async def start(self) -> None:
        if not self.redis_url:
            return
        try:
            from redis.asyncio import from_url

            self._redis = from_url(self.redis_url, decode_responses=True)
            await self._redis.ping()
            self.redis_healthy = True
        except Exception:  # noqa: BLE001
            self._redis = None
            self.redis_healthy = False

    async def close(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._redis = None

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        event = {
            "topic": topic,
            "ts": datetime.now(UTC).isoformat(),
            "payload": payload,
        }
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass
        if self._redis is not None:
            stream = STREAMS.get(topic, topic)
            try:
                await self._redis.xadd(
                    stream,
                    {"data": json.dumps(payload, separators=(",", ":"), default=str)},
                    maxlen=10_000,
                    approximate=True,
                )
                self.redis_healthy = True
            except Exception:  # noqa: BLE001
                self.redis_healthy = False

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self.max_queue)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    def health(self) -> dict[str, object]:
        return {
            "healthy": self.redis_healthy,
            "redis_configured": self.redis_configured,
            "subscribers": len(self._subscribers),
        }
