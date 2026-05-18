from __future__ import annotations

import asyncio
import copy
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from itertools import count
from typing import Any

from aiohttp import WSMsgType, web
from .app_keys import EVENT_BUS_KEY

ALLOWED_FILTER_KEYS = {"session_id", "task_id", "request_id", "agent_id", "group_id", "coordination_run_id"}


@dataclass(eq=False)
class Subscriber:
    filters: dict[str, str]
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=lambda: asyncio.Queue(maxsize=100))


class EventBus:
    def __init__(self, replay_limit: int = 500, replay_ttl_seconds: float = 21600.0):
        self._subs: set[Subscriber] = set()
        self.replay_limit = max(0, int(replay_limit))
        self.replay_ttl_seconds = max(0.001, float(replay_ttl_seconds))
        self._recent_by_session: dict[str, deque[tuple[float, int, dict[str, Any]]]] = {}
        self._recent_by_request: dict[str, deque[tuple[float, int, dict[str, Any]]]] = {}
        self._seq = count(1)

    def subscribe(self, filters: dict[str, str]) -> Subscriber:
        sub = Subscriber(filters={k: v for k, v in filters.items() if k in ALLOWED_FILTER_KEYS and v})
        self._subs.add(sub)
        return sub

    def unsubscribe(self, subscriber: Subscriber) -> None:
        self._subs.discard(subscriber)

    async def publish(self, event: dict[str, Any]) -> None:
        self._store_recent(event)
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

    def _event_value(self, event: dict[str, Any], key: str) -> str:
        value = event.get(key)
        if value:
            return str(value)
        data = event.get("data")
        if isinstance(data, dict) and data.get(key):
            return str(data[key])
        return ""

    def _append_limited(self, store: dict[str, deque[tuple[float, int, dict[str, Any]]]], key: str, item: tuple[float, int, dict[str, Any]]) -> None:
        if not key or self.replay_limit <= 0:
            return
        bucket = store.setdefault(key, deque())
        bucket.append(item)
        self._prune_bucket(bucket, now=time.time())
        while len(bucket) > self.replay_limit:
            bucket.popleft()

    def _prune_bucket(self, bucket: deque[tuple[float, int, dict[str, Any]]], *, now: float) -> None:
        cutoff = now - self.replay_ttl_seconds
        while bucket and bucket[0][0] < cutoff:
            bucket.popleft()

    def _store_recent(self, event: dict[str, Any]) -> None:
        if self.replay_limit <= 0 or not isinstance(event, dict):
            return
        now = time.time()
        item = (now, next(self._seq), copy.deepcopy(event))
        session_id = self._event_value(event, "session_id")
        request_id = self._event_value(event, "request_id")
        self._append_limited(self._recent_by_session, session_id, item)
        self._append_limited(self._recent_by_request, request_id, item)

    def recent_events(
        self,
        *,
        session_id: str = "",
        request_id: str = "",
        limit: int | None = None,
        types: set[str] | None = None,
        last_event_at: str | None = None,
    ) -> list[dict[str, Any]]:
        now = time.time()
        last_event_ts = _parse_event_time(last_event_at)
        buckets: list[deque[tuple[float, int, dict[str, Any]]]] = []
        if session_id:
            bucket = self._recent_by_session.get(session_id)
            if bucket is not None:
                self._prune_bucket(bucket, now=now)
                buckets.append(bucket)
        if request_id:
            bucket = self._recent_by_request.get(request_id)
            if bucket is not None:
                self._prune_bucket(bucket, now=now)
                buckets.append(bucket)
        if not buckets:
            return []
        seen: set[int] = set()
        items: list[tuple[float, int, dict[str, Any]]] = []
        for bucket in buckets:
            for ts, seq, event in bucket:
                if seq in seen:
                    continue
                seen.add(seq)
                event_type = str(event.get("type") or event.get("event_type") or "")
                if types and event_type not in types:
                    continue
                event_ts = _event_sort_time(event, ts)
                if last_event_ts is not None and event_ts <= last_event_ts:
                    continue
                items.append((ts, seq, event))
        items.sort(key=lambda item: (_event_sort_time(item[2], item[0]), item[1]))
        max_items = self.replay_limit if limit is None else max(0, min(int(limit), self.replay_limit))
        if max_items:
            items = items[-max_items:]
        else:
            items = []
        replayed: list[dict[str, Any]] = []
        for _ts, _seq, event in items:
            copy_event = copy.deepcopy(event)
            metadata = copy_event.get("metadata") if isinstance(copy_event.get("metadata"), dict) else {}
            copy_event["metadata"] = {**metadata, "replayed": True}
            data = copy_event.get("data") if isinstance(copy_event.get("data"), dict) else {}
            copy_event["data"] = {**data, "replayed": True}
            replayed.append(copy_event)
        return replayed


def _parse_types(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def _parse_event_time(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw).timestamp()
    except Exception:
        return None


def _event_sort_time(event: dict[str, Any], fallback: float) -> float:
    parsed = _parse_event_time(str(event.get("created_at") or ""))
    return parsed if parsed is not None else fallback


async def events_ws_handler(request: web.Request) -> web.WebSocketResponse:
    bus: EventBus = request.app[EVENT_BUS_KEY]
    filters = {k: request.query.get(k, "") for k in ALLOWED_FILTER_KEYS}
    types = _parse_types(request.query.get("types"))
    sub = bus.subscribe(filters)
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    await ws.send_json({"type": "connected", "engine": "opencode"})
    replay_enabled = str(request.query.get("replay", "")).lower() in {"1", "true", "yes", "on"}
    if replay_enabled:
        try:
            replay_limit = int(request.query.get("replay_limit", "") or bus.replay_limit)
        except ValueError:
            replay_limit = bus.replay_limit
        for event in bus.recent_events(
            session_id=filters.get("session_id", ""),
            request_id=filters.get("request_id", ""),
            limit=replay_limit,
            types=types or None,
            last_event_at=request.query.get("last_event_at"),
        ):
            await ws.send_json(event)

    async def _sender() -> None:
        while True:
            event = await sub.queue.get()
            event_type = str(event.get("type") or event.get("event_type") or "")
            if types and event_type not in types:
                continue
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
