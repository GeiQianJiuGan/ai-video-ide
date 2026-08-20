"""单一 WebSocket 端点 + 频道订阅。"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.core.config import settings
from app.core.logging import get_logger
from app.events.bus import Channel, bus

router = APIRouter()
log = get_logger("api.ws")

HEARTBEAT_SECONDS = 20


def _parse_channels(raw: str | None) -> set[Channel] | None:
    if not raw:
        return None
    picked: set[Channel] = set()
    for name in raw.split(","):
        name = name.strip()
        if not name:
            continue
        try:
            picked.add(Channel(name))
        except ValueError:
            log.warning("ws.unknown_channel", channel=name)
    return picked or None


@router.websocket("/ws")
async def ws_endpoint(
    websocket: WebSocket,
    project_id: str | None = Query(default=None),
    channels: str | None = Query(default=None),
    token: str | None = Query(default=None),
) -> None:
    if settings.require_handshake and token != settings.handshake_token:
        await websocket.close(code=4401, reason="invalid handshake token")
        return

    await websocket.accept()
    selected = _parse_channels(channels)
    await websocket.send_json(
        {
            "channel": "system",
            "event": "system.connected",
            "payload": {
                "version": settings.version,
                "project_id": project_id,
                "channels": sorted(c.value for c in selected) if selected else "all",
            },
        }
    )

    async def pump() -> None:
        async for ev in bus.subscribe(project_id=project_id, channels=selected):
            await websocket.send_json(ev.to_dict())

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await websocket.send_json({"channel": "system", "event": "system.ping", "payload": {}})

    async def drain() -> None:
        # 客户端消息目前只用于保持连接活跃；订阅变更请重新建立连接。
        while True:
            await websocket.receive_text()

    tasks = [asyncio.create_task(c()) for c in (pump, heartbeat, drain)]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            task.result()
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log.warning("ws.error", error=f"{type(exc).__name__}: {exc}")
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
