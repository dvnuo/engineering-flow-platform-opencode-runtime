from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from aiohttp import WSMsgType, web
from .app_keys import *

ALLOWED_FILTER_KEYS = {"session_id", "task_id", "request_id", "agent_id", "group_id", "coordination_run_id"}


@dataclass(eq=False)
class Subscriber:
    filters: dict[str, str]
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=lambda: asyncio.Queue(maxsize=100))


class EventBus:
    def __init__(self):
        self._subs: set[Subscriber] = set()

    def subscribe(self, filters: dict[str, str]) -> Subscriber:
        sub = Subscriber(filters={k: v for k, v in filters.items() if k in ALLOWED_FILTER_KEYS and v})
        self._subs.add(sub)
        return sub

    def unsubscribe(self, subscriber: Subscriber) -> None:
        self._subs.discard(subscriber)

    async def publish(self, event: dict[str, Any]) -> None:
        for sub in list(self._subs):
            if not self._matches(sub.filters, event):
                continue
            if sub.queue.full():
                try:
                    sub.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def _matches(self, filters: dict[str, str], event: dict[str, Any]) -> bool:
        for key, value in filters.items():
            if key not in event or str(event.get(key)) != value:
                return False
        return True


async def events_ws_handler(request: web.Request) -> web.WebSocketResponse:
    bus: EventBus = request.app[EVENT_BUS_KEY]
    filters = {k: request.query.get(k, "") for k in ALLOWED_FILTER_KEYS}
    sub = bus.subscribe(filters)
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    await ws.send_json({"type": "connected", "engine": "opencode"})

    async def _sender() -> None:
        while True:
            event = await sub.queue.get()
            await ws.send_json(event)

    sender_task = asyncio.create_task(_sender())
    try:
        async for msg in ws:
            if msg.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                break
    finally:
        bus.unsubscribe(sub)
        sender_task.cancel()
        await asyncio.gather(sender_task, return_exceptions=True)
    return ws
