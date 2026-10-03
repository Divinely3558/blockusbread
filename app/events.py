"""进程内事件总线：挂载状态变化推给所有 SSE 客户端。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger("events")


class EventBus:
    """每个订阅者一个独立队列；慢客户端丢弃旧消息，不阻塞发布者。"""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        self._subscribers.add(queue)
        log.debug("SSE 订阅者加入，当前 %d 个", len(self._subscribers))
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    async def publish(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        message = {"type": event_type, "data": data or {}}
        stale: list[asyncio.Queue[dict[str, Any]]] = []
        for queue in self._subscribers:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # 丢弃最旧一条后再放，保证慢客户端也能拿到最新状态
                try:
                    queue.get_nowait()
                    queue.put_nowait(message)
                except asyncio.QueueEmpty:
                    stale.append(queue)
        for queue in stale:
            self._subscribers.discard(queue)
