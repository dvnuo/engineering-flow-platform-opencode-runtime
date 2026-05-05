from __future__ import annotations

import asyncio
import time
from typing import Any

from .thinking_events import safe_preview, utc_now_iso


def _canonical(raw_event: dict[str, Any]) -> dict[str, Any]:
    payload = raw_event.get("payload")
    if isinstance(payload, dict):
        return payload
    return raw_event


def _event_type(raw_event: dict[str, Any], canonical: dict[str, Any]) -> str:
    for key in ("type", "event"):
        value = canonical.get(key)
        if value:
            return str(value).lower()
    for key in ("type", "event"):
        value = raw_event.get(key)
        if value:
            return str(value).lower()
    return ""


def _collect_strings(value: Any, out: dict[str, str]) -> None:
    if isinstance(value, dict):
        for key, val in value.items():
            if isinstance(val, str) and val and key not in out:
                out[key] = val
            _collect_strings(val, out)
    elif isinstance(value, list):
        for item in value:
            _collect_strings(item, out)


def _map_task_id(task_store, opencode_session_id: str, message_ids: set[str]) -> str | None:
    if not opencode_session_id:
        return None
    matched = []
    for rec in task_store.list_all():
        if rec.opencode_session_id != opencode_session_id:
            continue
        if message_ids and (rec.opencode_message_id in message_ids or rec.opencode_prompt_id in message_ids):
            return rec.task_id
        if rec.status in {"running", "accepted"}:
            matched.append(rec)
    if not message_ids and len(matched) == 1:
        return matched[0].task_id
    return None


def normalize_opencode_event(raw_event: dict[str, Any], *, session_store, task_store, settings) -> dict[str, Any] | None:
    if not isinstance(raw_event, dict):
        return None
    canonical = _canonical(raw_event)
    raw_type = _event_type(raw_event, canonical)
    values: dict[str, str] = {}
    _collect_strings(raw_event, values)
    opencode_session_id = values.get("opencode_session_id") or values.get("session_id") or values.get("sessionID") or values.get("sessionId") or ""
    request_id = values.get("requestID") or values.get("permissionID") or values.get("permission_id") or values.get("id") or ""
    message_ids = {values.get(k, "") for k in ("message_id", "messageID", "messageId", "parentID", "parent_id") if values.get(k)}
    mapped = session_store.find_by_opencode_session_id(opencode_session_id) if opencode_session_id else None
    session_id = mapped.portal_session_id if mapped else (opencode_session_id or "")
    normalized_type = f"opencode.{raw_type}" if raw_type else "opencode.event"
    if "permission" in raw_type and any(t in raw_type for t in ("asked", "requested", "created", "pending", "updated")):
        normalized_type = "permission_request"
    elif "permission" in raw_type and any(t in raw_type for t in ("replied", "resolved", "approved", "denied", "rejected", "closed")):
        normalized_type = "permission_resolved"
    elif "tool" in raw_type and any(t in raw_type for t in ("start", "call", "begin", "running")):
        normalized_type = "tool.started"
    elif "tool" in raw_type and any(t in raw_type for t in ("complete", "result", "end", "finish", "success")):
        normalized_type = "tool.completed"
    elif "tool" in raw_type and any(t in raw_type for t in ("fail", "error")):
        normalized_type = "tool.failed"
    elif raw_type == "message.part.updated":
        normalized_type = "assistant_delta"
    elif raw_type in {"message.completed", "message.finished"}:
        normalized_type = "message.completed"
    elif raw_type.startswith("session."):
        normalized_type = "session.updated"

    task_id = _map_task_id(task_store, opencode_session_id, message_ids)
    data = {
        "raw_event_preview": safe_preview(raw_event, settings.event_bridge_event_preview_chars),
        "canonical_preview": safe_preview(canonical, settings.event_bridge_event_preview_chars),
        "permission_id": request_id,
        "delta": values.get("delta", ""),
        "message": values.get("message", ""),
        "tool": values.get("tool", values.get("tool_name", "")),
        "input_preview": values.get("input", ""),
        "output_preview": values.get("output", ""),
        "risk_level": values.get("risk_level", "medium"),
    }
    evt = {
        "type": normalized_type,
        "event_type": normalized_type,
        "engine": "opencode",
        "raw_type": raw_type,
        "session_id": session_id,
        "opencode_session_id": opencode_session_id,
        "request_id": request_id,
        "state": "received",
        "summary": normalized_type,
        "data": safe_preview(data, settings.event_bridge_event_preview_chars),
        "created_at": utc_now_iso(),
        "ts": time.time(),
    }
    if task_id:
        evt["task_id"] = task_id
    if normalized_type.startswith("permission_"):
        evt["permission_id"] = request_id
    return evt


class OpenCodeEventBridge:
    def __init__(self, settings, client, event_bus, session_store, task_store, chatlog_store=None):
        self.settings = settings
        self.client = client
        self.event_bus = event_bus
        self.session_store = session_store
        self.task_store = task_store
        self.chatlog_store = chatlog_store
        self.enabled = True
        self.running = False
        self.connected = False
        self.reconnects = 0
        self.last_event_at = None
        self.last_error = None
        self.last_raw_type = ""

    def status_snapshot(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "running": self.running, "connected": self.connected, "reconnects": self.reconnects, "last_event_at": self.last_event_at, "last_error": self.last_error, "last_raw_type": self.last_raw_type}

    async def publish_raw_event(self, raw_event: dict) -> dict | None:
        event = normalize_opencode_event(raw_event, session_store=self.session_store, task_store=self.task_store, settings=self.settings)
        if not event:
            return None
        self.last_event_at = event.get("created_at")
        self.last_raw_type = event.get("raw_type", "")
        await self.event_bus.publish(event)
        task_id = event.get("task_id")
        if task_id:
            try:
                self.task_store.append_event(task_id, event)
            except Exception:
                pass
        if self.chatlog_store and event.get("session_id") and event.get("request_id"):
            try:
                self.chatlog_store.append_event(event["session_id"], request_id=event["request_id"], event=event, runtime=True)
            except Exception:
                pass
        return event

    async def run_forever(self):
        self.running = True
        backoff = self.settings.event_bridge_initial_backoff_seconds
        try:
            while True:
                try:
                    self.connected = True
                    async for raw in self.client.event_stream(global_events=True, timeout_seconds=None):
                        await self.publish_raw_event(raw)
                    self.connected = False
                    self.reconnects += 1
                    await asyncio.sleep(backoff)
                    backoff = min(self.settings.event_bridge_max_backoff_seconds, backoff * 2)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.connected = False
                    self.last_error = safe_preview(str(exc), 300)
                    await self.event_bus.publish({"type": "event_bridge.disconnected", "event_type": "event_bridge.disconnected", "engine": "opencode", "created_at": utc_now_iso(), "ts": time.time(), "error": self.last_error})
                    self.reconnects += 1
                    await asyncio.sleep(backoff)
                    backoff = min(self.settings.event_bridge_max_backoff_seconds, backoff * 2)
        finally:
            self.running = False
            self.connected = False
