from __future__ import annotations

import asyncio
import time
from typing import Any

from .profile_store import sanitize_public_secrets
from .thinking_events import safe_preview, utc_now_iso


def _sanitize_event_value(value: Any, max_chars: int) -> Any:
    sanitized = sanitize_public_secrets(value)
    return safe_preview(sanitized, max_chars)


def _sanitize_event_text(value: Any, max_chars: int = 300) -> str:
    sanitized = _sanitize_event_value(value, max_chars)
    if isinstance(sanitized, str):
        return sanitized
    if sanitized in (None, ""):
        return ""
    return "[redacted]"


def _canonical(raw_event: dict[str, Any]) -> dict[str, Any]:
    payload = raw_event.get("payload")
    if isinstance(payload, dict):
        canonical = dict(payload)
        props = canonical.get("properties")
        if isinstance(props, dict):
            canonical.update({k: v for k, v in props.items() if k not in canonical})
        return canonical
    data = raw_event.get("data")
    if isinstance(data, dict):
        return data
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


def _first_string(values: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = values.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _status_value(values: dict[str, str]) -> str:
    return _first_string(values, "status", "state", "decision", "resolution", "action", "answer").lower()


def _tool_name(values: dict[str, str]) -> str:
    return _first_string(values, "tool", "tool_name", "toolName", "name", "command", "callName")


def _message_text(canonical: dict[str, Any], values: dict[str, str]) -> str:
    for key in ("delta", "text", "message", "content"):
        if values.get(key):
            return values[key]
    part = canonical.get("part")
    if isinstance(part, dict) and isinstance(part.get("text"), str):
        return part["text"]
    message = canonical.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
        text = message.get("text")
        if isinstance(text, str):
            return text
    output = canonical.get("output")
    if isinstance(output, dict) and isinstance(output.get("text"), str):
        return output["text"]
    return ""


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
    max_chars = settings.event_bridge_event_preview_chars
    canonical = _canonical(raw_event)
    raw_type = _event_type(raw_event, canonical)
    values: dict[str, str] = {}
    _collect_strings(raw_event, values)
    _collect_strings(canonical, values)

    opencode_session_id = _first_string(values, "opencode_session_id", "session_id", "sessionID", "sessionId")
    permission_id = _first_string(values, "permissionID", "permission_id", "requestID", "request_id", "id")
    request_id = _first_string(values, "requestID", "request_id", "id")
    tool = _tool_name(values)
    input_preview = _first_string(values, "input", "arguments", "args", "params", "command")
    output_preview = _first_string(values, "output", "result", "response")
    text = _message_text(canonical, values)
    risk_level = _first_string(values, "risk_level", "riskLevel") or "medium"
    status = _status_value(values)

    message_ids = {values.get(k, "") for k in ("message_id", "messageID", "messageId", "parentID", "parent_id") if values.get(k)}
    mapped = session_store.find_by_opencode_session_id(opencode_session_id) if opencode_session_id else None
    session_id = mapped.portal_session_id if mapped else (opencode_session_id or "")
    normalized_type = f"opencode.{raw_type}" if raw_type else "opencode.event"

    if "permission" in raw_type:
        if any(x in raw_type for x in ("replied", "resolved", "approved", "denied", "rejected", "closed")):
            normalized_type = "permission_resolved"
        elif raw_type.endswith("updated") or "updated" in raw_type:
            if status in {"approved", "allow", "allowed", "accepted", "granted", "denied", "deny", "rejected", "refused", "blocked", "resolved", "replied", "closed"}:
                normalized_type = "permission_resolved"
            else:
                normalized_type = "permission_request"
        elif any(x in raw_type for x in ("asked", "requested", "created", "pending")):
            normalized_type = "permission_request"
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
    s_permission_id = _sanitize_event_value(permission_id, 300)
    s_tool = _sanitize_event_value(tool, 300)
    s_input = _sanitize_event_value(input_preview, max_chars)
    s_output = _sanitize_event_value(output_preview, max_chars)
    s_risk = _sanitize_event_value(risk_level, 100)
    s_text = _sanitize_event_value(text, max_chars)
    s_status = _sanitize_event_value(status, 100)

    data = {
        "raw_event_preview": _sanitize_event_value(raw_event, max_chars),
        "canonical_preview": _sanitize_event_value(canonical, max_chars),
        "permission_id": s_permission_id,
        "tool": s_tool,
        "input_preview": s_input,
        "output_preview": s_output,
        "risk_level": s_risk,
        "delta": s_text,
        "message": s_text,
        "status": s_status,
    }

    s_session_id = _sanitize_event_text(session_id, 300)
    s_opencode_session_id = _sanitize_event_text(opencode_session_id, 300)
    s_request_id = _sanitize_event_text(request_id, 300)
    s_raw_type = _sanitize_event_text(raw_type, 200)

    evt = {
        "type": normalized_type,
        "event_type": normalized_type,
        "engine": "opencode",
        "raw_type": s_raw_type,
        "session_id": s_session_id,
        "opencode_session_id": s_opencode_session_id,
        "request_id": s_request_id,
        "state": "received",
        "summary": normalized_type,
        "data": data,
        "created_at": utc_now_iso(),
        "ts": time.time(),
    }
    if task_id:
        evt["task_id"] = task_id
    if normalized_type.startswith("permission_"):
        evt["permission_id"] = s_permission_id
        evt["tool"] = s_tool
        evt["input_preview"] = s_input
        evt["risk_level"] = s_risk
        if normalized_type == "permission_resolved":
            evt["decision"] = s_status or _sanitize_event_value(_first_string(values, "decision", "resolution", "answer"), 100)
    if normalized_type.startswith("tool."):
        evt["tool"] = s_tool
        if input_preview:
            evt["input_preview"] = s_input
        if output_preview:
            evt["output_preview"] = s_output
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
                    self.last_error = _sanitize_event_value(str(exc), 300)
                    if not isinstance(self.last_error, str):
                        self.last_error = "[redacted]"
                    await self.event_bus.publish({"type": "event_bridge.disconnected", "event_type": "event_bridge.disconnected", "engine": "opencode", "created_at": utc_now_iso(), "ts": time.time(), "error": self.last_error})
                    self.reconnects += 1
                    await asyncio.sleep(backoff)
                    backoff = min(self.settings.event_bridge_max_backoff_seconds, backoff * 2)
        finally:
            self.running = False
            self.connected = False
