"""进程内事件总线 → WebSocket 广播。

约定（见 docs/03 §5）：事件幂等、可丢失；前端重连后必须调 REST 做全量对齐，
因此这里不做任何投递保证，也不做事件持久化。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from app.core.logging import get_logger

log = get_logger("events")

QUEUE_MAXSIZE = 256


class Channel(StrEnum):
    JOB = "job"
    QUEUE = "queue"
    SHOT = "shot"
    VERSION = "version"
    ASSET = "asset"
    SYSTEM = "system"
    ERROR = "error"


@dataclass(slots=True)
class Event:
    channel: Channel
    event: str
    payload: dict[str, Any]
    project_id: str | None = None
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel.value,
            "event": self.event,
            "project_id": self.project_id,
            "ts": self.ts,
            "payload": self.payload,
        }


@dataclass(slots=True, eq=False)  # eq=False 保留默认 __hash__，才能放进 set
class _Subscriber:
    queue: asyncio.Queue[Event]
    loop: asyncio.AbstractEventLoop
    project_id: str | None
    channels: set[Channel] | None
    dropped: int = 0

    def wants(self, ev: Event) -> bool:
        if self.channels is not None and ev.channel not in self.channels:
            return False
        if self.project_id and ev.project_id and ev.project_id != self.project_id:
            return False
        return True

    def deliver(self, ev: Event) -> None:
        """必须在 self.loop 所在线程调用。队列满时丢最旧——慢客户端不得拖慢生成流程。"""
        if self.queue.full():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover
                pass
            self.dropped += 1
        self.queue.put_nowait(ev)


class EventBus:
    def __init__(self) -> None:
        self._subs: set[_Subscriber] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def publish(self, ev: Event) -> None:
        """非阻塞广播，可从任意线程调用。

        asyncio.Queue 不是线程安全的：FFmpeg 进度解析等跑在 executor 线程里的代码
        若直接 put_nowait，订阅者的 await 不会被唤醒。因此跨线程时统一走
        call_soon_threadsafe 投递到订阅者自己的事件循环。
        """
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None

        for sub in tuple(self._subs):
            if not sub.wants(ev):
                continue
            if sub.loop is current:
                sub.deliver(ev)
            elif not sub.loop.is_closed():
                sub.loop.call_soon_threadsafe(sub.deliver, ev)

    def emit(
        self,
        channel: Channel,
        event: str,
        payload: dict[str, Any],
        project_id: str | None = None,
    ) -> None:
        self.publish(Event(channel=channel, event=event, payload=payload, project_id=project_id))

    async def subscribe(
        self,
        project_id: str | None = None,
        channels: set[Channel] | None = None,
    ) -> AsyncIterator[Event]:
        sub = _Subscriber(
            queue=asyncio.Queue(maxsize=QUEUE_MAXSIZE),
            loop=asyncio.get_running_loop(),
            project_id=project_id,
            channels=channels,
        )
        self._subs.add(sub)
        log.debug("ws.subscribe", project_id=project_id, total=len(self._subs))
        try:
            while True:
                yield await sub.queue.get()
        finally:
            self._subs.discard(sub)
            if sub.dropped:
                log.warning("ws.events_dropped", dropped=sub.dropped)
            log.debug("ws.unsubscribe", total=len(self._subs))


bus = EventBus()
