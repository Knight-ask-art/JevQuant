"""WebSocket event stream.

Browsers cannot set custom headers on a WebSocket handshake, so the API key
is accepted as a query parameter when auth is enabled. The stream is
read-only and carries the same payloads as the Redis streams.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .security import websocket_authorized

router = APIRouter()

HEARTBEAT_SECONDS = 15.0


@router.websocket("/ws/events")
async def events(websocket: WebSocket) -> None:
    if not await websocket_authorized(websocket):
        await websocket.close(code=4401, reason="unauthorized")
        return
    container = websocket.app.state.container
    await websocket.accept()
    await websocket.send_json(
        {
            "topic": "system.events",
            "payload": {
                "event": "stream.open",
                "mode": (await container.modes.current_mode()).value,
                "data_source": container.market.source,
            },
        }
    )
    async with container.broker.subscribe() as queue:
        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=HEARTBEAT_SECONDS
                    )
                except asyncio.TimeoutError:
                    await websocket.send_json(
                        {"topic": "system.events", "payload": {"event": "heartbeat"}}
                    )
                    continue
                await websocket.send_json(event)
        except (WebSocketDisconnect, RuntimeError):
            return
        except Exception:  # noqa: BLE001
            try:
                await websocket.close(code=1011)
            except Exception:  # noqa: BLE001
                pass
            return
