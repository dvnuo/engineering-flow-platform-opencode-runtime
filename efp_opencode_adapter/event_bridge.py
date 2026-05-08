from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any

from .index_loader import load_tools_index
from .profile_store import sanitize_public_secrets
from .thinking_events import safe_preview, utc_now_iso
from .trace_context import add_trace_context, build_trace_context
from .opencode_message_adapter import extract_visible_text_from_parts, extract_reasoning_texts_from_parts


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
        if payload.get("type") == "sync" and isinstance(payload.get("syncEvent"), dict):
            sync = payload["syncEvent"]
            data = sync.get("data") if isinstance(sync.get("data"), dict) else {}
            canonical = dict(data)
            canonical["type"] = sync.get("type") or payload.get("type")
            canonical["opencode_event_id"] = sync.get("id")
            return canonical
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
    value = ""
    for key in ("type", "event"):
        v = canonical.get(key)
        if v:
            value = str(v).lower()
            break
    if not value:
        for key in ("type", "event"):
            v = raw_event.get(key)
            if v:
                value = str(v).lower()
                break
    if value.startswith("message.part.updated"):
        return "message.part.updated"
    if value.startswith("message.part.delta"):
        return "message.part.delta"
    return value


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
    return _first_string(values, "status", "state", "decision", "resolution", "response", "action", "answer").lower()


def _tool_name(values: dict[str, str]) -> str:
    return _first_string(values, "tool", "tool_name", "toolName", "name", "command", "callName")


def _message_text(canonical: dict[str, Any], values: dict[str, str]) -> str:
    part = canonical.get("part")
    if isinstance(part, dict):
        return extract_visible_text_from_parts([part])
    parts = canonical.get("parts")
    if isinstance(parts, list):
        text = extract_visible_text_from_parts(parts)
        if text:
            return text
    for key in ("delta", "text", "message", "content"):
        if values.get(key):
            return values[key]
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


def _build_tool_metadata(settings) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for tool in load_tools_index(settings).get("tools", []):
        if not isinstance(tool, dict):
            continue
        for key in ("name", "opencode_name", "legacy_name", "native_name", "capability_id", "tool_id"):
            value = tool.get(key)
            if isinstance(value, str) and value:
                if value not in out:
                    out[value] = tool
                elif not bool(out[value].get("enabled", True)) and bool(tool.get("enabled", True)):
                    out[value] = tool
    return out




def _safe_policy_tags(meta: dict[str, Any]) -> list[str]:
    raw = meta.get("policy_tags") or []
    if isinstance(raw, list):
        candidates = raw
    elif isinstance(raw, (str, int, float, bool)):
        candidates = [raw]
    else:
        candidates = []
    out: list[str] = []
    for item in candidates:
        if isinstance(item, (str, int, float, bool)):
            value = safe_preview(str(item), 100)
            if isinstance(value, str) and value:
                out.append(value)
    return out

def _is_mutation_tool(meta: dict[str, Any]) -> bool:
    tags = {str(x).lower() for x in _safe_policy_tags(meta)}
    risk = str(meta.get("risk_level") or "").lower()
    return bool(meta.get("mutation") is True or {"mutation", "write", "writeback", "external_write", "destructive"} & tags or risk in {"high", "critical"})


_BUILTIN_TOOLS = {"bash", "read", "edit", "write", "grep", "glob", "webfetch", "websearch", "skill", "todowrite", "question"}

def _raw_session_id_from_event(raw_event: dict[str, Any], canonical: dict[str, Any], values: dict[str, str]) -> str:
    return _first_string(values, "opencode_session_id", "session_id", "sessionID", "sessionId")


def _raw_message_id_from_event(canonical: dict[str, Any], values: dict[str, str]) -> str:
    return _first_string(values, "messageID", "message_id", "messageId", "id")


def _raw_part_id_from_event(canonical: dict[str, Any], values: dict[str, str]) -> str:
    return _first_string(values, "partID", "part_id", "partId", "id")


def _cache_put_limited(cache: OrderedDict, key: tuple[str, ...], value: Any, limit: int) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


def _extract_info_from_message_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    info = payload.get("info")
    return info if isinstance(info, dict) else {}


def _extract_parts_from_message_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    parts = payload.get("parts")
    if isinstance(parts, list):
        return [p for p in parts if isinstance(p, dict)]
    message = payload.get("message")
    if isinstance(message, dict) and isinstance(message.get("parts"), list):
        return [p for p in message["parts"] if isinstance(p, dict)]
    return []


def _bool_true(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return False


def _part_meta_from_part(part: dict[str, Any]) -> dict[str, Any]:
    return {"type": str(part.get("type") or "").lower(), "ignored": _bool_true(part.get("ignored")), "synthetic": _bool_true(part.get("synthetic"))}


def normalize_opencode_event(raw_event: dict[str, Any], *, session_store, task_store, settings, tool_metadata: dict[str, dict[str, Any]] | None = None, message_role: str | None = None, part_meta: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if not isinstance(raw_event, dict):
        return None
    max_chars = settings.event_bridge_event_preview_chars
    canonical = _canonical(raw_event)
    raw_type = _event_type(raw_event, canonical)
    values: dict[str, str] = {}
    _collect_strings(raw_event, values)
    _collect_strings(canonical, values)

    opencode_session_id = _raw_session_id_from_event(raw_event, canonical, values)
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
        part = canonical.get("part") if isinstance(canonical.get("part"), dict) else {}
        ptype = str(part.get("type") or "").lower()
        if ptype == "tool":
            normalized_type = "tool.started" if status in {"", "started", "running"} else "tool.completed"
        elif ptype == "step-start":
            normalized_type = "opencode.step.started"
        elif ptype == "step-finish":
            normalized_type = "opencode.step.finished"
        else:
            normalized_type = "opencode.message.part.updated"
        text = ""
    elif raw_type == "message.part.delta":
        message_id = _raw_message_id_from_event(canonical, values)
        part_id = _raw_part_id_from_event(canonical, values)
        field = str(canonical.get("field") or values.get("field") or "").lower()
        delta = canonical.get("delta") or values.get("delta") or ""
        delta = delta if isinstance(delta, str) else ""
        normalized_type = "opencode.message.part.delta"
        text = ""
        role = str(message_role or "").lower()
        pmeta = part_meta if isinstance(part_meta, dict) else {}
        ptype = str(pmeta.get("type") or "").lower()
        ignored = _bool_true(pmeta.get("ignored"))
        synthetic = _bool_true(pmeta.get("synthetic"))
        if field == "text" and delta and role == "assistant" and ptype in {"text", "reasoning"} and not ignored and not synthetic:
            if ptype == "text":
                normalized_type = "message.delta"
                text = delta
            elif ptype == "reasoning":
                normalized_type = "llm_thinking"
                text = delta
        values["__part_field"] = field
        values["__part_message_id"] = message_id
        values["__part_id"] = part_id
        values["__message_role"] = role
        values["__part_type"] = ptype
    elif raw_type in {"message.completed", "message.finished"}:
        normalized_type = "message.completed"
    elif raw_type.startswith("session."):
        normalized_type = "session.updated"
    retry_status = canonical.get("status") if isinstance(canonical.get("status"), dict) else {}
    if raw_type == "session.status" and isinstance(retry_status, dict) and str(retry_status.get("type", "")).lower() == "retry":
        normalized_type = "provider.retry"

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
        "status": s_status,
    }
    if normalized_type in {"assistant_delta", "message.delta"} and s_text:
        data["delta"] = s_text
        data["message"] = s_text
    elif normalized_type in {"llm_thinking", "opencode.reasoning"} and s_text:
        data["message"] = s_text
    data["message_role"] = _sanitize_event_text(values.get("__message_role") or message_role or "", 100)
    data["part_type"] = _sanitize_event_text(values.get("__part_type") or (part_meta or {}).get("type") or "", 100)
    data["field"] = _sanitize_event_text(values.get("__part_field") or values.get("field") or "", 100)
    data["message_id"] = _sanitize_event_text(values.get("__part_message_id") or _raw_message_id_from_event(canonical, values), 300)
    data["part_id"] = _sanitize_event_text(values.get("__part_id") or _raw_part_id_from_event(canonical, values), 300)

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
    if normalized_type == "provider.retry":
        evt["state"] = "retrying"
        evt["summary"] = "Provider API retry"
        evt["data"]["message"] = _sanitize_event_value(retry_status.get("message"), max_chars)
        evt["data"]["attempt"] = retry_status.get("attempt")
        evt["data"]["next"] = retry_status.get("next")
        evt["data"]["raw_type"] = s_raw_type
        evt["data"]["diagnostic_hint"] = "OpenCode provider API retrying. Check runtime profile LLM provider/model/api_key/base_url/proxy."
    if task_id:
        evt["task_id"] = task_id
    if normalized_type.startswith("permission_"):
        evt["permission_id"] = s_permission_id
        evt["tool"] = s_tool
        evt["input_preview"] = s_input
        evt["risk_level"] = s_risk
        if normalized_type == "permission_resolved":
            evt["decision"] = s_status or _sanitize_event_value(_first_string(values, "decision", "resolution", "response", "answer"), 100)
    if normalized_type.startswith("tool."):
        evt["tool"] = s_tool
        if input_preview:
            evt["input_preview"] = s_input
        if output_preview:
            evt["output_preview"] = s_output
        meta = (tool_metadata or {}).get(str(tool)) or (tool_metadata or {}).get(str(s_tool))
        if isinstance(meta, dict):
            mutation = _is_mutation_tool(meta)
            risk_level = str(meta.get("risk_level") or s_risk or "medium")
            requires_identity = bool(meta.get("requires_identity_binding"))
            audit_event = mutation or requires_identity or risk_level.lower() in {"high", "critical"}
            extra = {
                "capability_id": _sanitize_event_value(meta.get("capability_id"), 200),
                "policy_tags": _safe_policy_tags(meta),
                "risk_level": _sanitize_event_value(risk_level, 100),
                "requires_identity_binding": requires_identity,
                "mutation": mutation,
                "audit_event": audit_event,
                "tool_source_ref": _sanitize_event_value(meta.get("source_ref"), 200),
            }
            evt.update(extra)
            evt["data"].update(extra)
        else:
            extra = {"capability_id": None, "policy_tags": [], "risk_level": s_risk or "medium", "requires_identity_binding": False, "mutation": False, "audit_event": False, "tool_source_ref": None}
            evt.update(extra)
            evt["data"].update(extra)
        meta = (tool_metadata or {}).get(str(tool)) or (tool_metadata or {}).get(str(s_tool))
        if isinstance(meta, dict):
            tool_source = meta.get("source_ref") or "tools_repo"
        elif str(tool).lower() in _BUILTIN_TOOLS:
            tool_source = "opencode_builtin"
        else:
            tool_source = "unknown"
        evt["tool_name"] = s_tool
    else:
        meta = (tool_metadata or {}).get(str(tool)) or (tool_metadata or {}).get(str(s_tool))
        if isinstance(meta, dict):
            tool_source = meta.get("source_ref") or "tools_repo"
        elif str(tool).lower() in _BUILTIN_TOOLS:
            tool_source = "opencode_builtin"
        else:
            tool_source = "unknown"
    trace_context = build_trace_context(settings, request_id=s_request_id, session_id=s_session_id, task_id=task_id or "", opencode_session_id=s_opencode_session_id, tool_name=s_tool if (normalized_type.startswith("tool.") or normalized_type.startswith("permission_")) else "", tool_source=tool_source)
    add_trace_context(evt, trace_context)
    evt["tool_source"] = _sanitize_event_text(tool_source, 200)
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
        self.tool_metadata = _build_tool_metadata(settings)
        self._message_roles: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._part_meta: OrderedDict[tuple[str, str, str], dict[str, Any]] = OrderedDict()
        self._cache_limit = 5000

    def refresh_tool_metadata(self) -> dict[str, dict[str, Any]]:
        self.tool_metadata = _build_tool_metadata(self.settings)
        return self.tool_metadata

    def status_snapshot(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "running": self.running, "connected": self.connected, "reconnects": self.reconnects, "last_event_at": self.last_event_at, "last_error": self.last_error, "last_raw_type": self.last_raw_type}

    async def publish_raw_event(self, raw_event: dict) -> dict | None:
        canonical = _canonical(raw_event)
        raw_type = _event_type(raw_event, canonical)
        values: dict[str, str] = {}
        _collect_strings(raw_event, values)
        _collect_strings(canonical, values)
        session_id = _raw_session_id_from_event(raw_event, canonical, values)
        message_id = _raw_message_id_from_event(canonical, values)
        part_id = _raw_part_id_from_event(canonical, values)
        message_role = None
        part_meta = None

        if raw_type == "message.updated":
            info = canonical.get("info") if isinstance(canonical.get("info"), dict) else {}
            if info:
                message_id = str(info.get("id") or message_id or "")
                role = str(info.get("role") or "").lower()
                info_session = str(info.get("sessionID") or info.get("sessionId") or session_id or "")
                if info_session and message_id and role:
                    _cache_put_limited(self._message_roles, (info_session, message_id), role, self._cache_limit)
        elif raw_type == "message.part.updated":
            part = canonical.get("part") if isinstance(canonical.get("part"), dict) else {}
            p_message_id = str(part.get("messageID") or part.get("messageId") or part.get("message_id") or message_id or "")
            p_part_id = str(part.get("id") or part_id or "")
            if session_id and p_message_id and p_part_id:
                _cache_put_limited(self._part_meta, (session_id, p_message_id, p_part_id), _part_meta_from_part(part), self._cache_limit)
        elif raw_type == "message.part.delta":
            if session_id and message_id:
                message_role = self._message_roles.get((session_id, message_id))
            if session_id and message_id and part_id:
                part_meta = self._part_meta.get((session_id, message_id, part_id))
            if (not message_role or not part_meta) and session_id and message_id and hasattr(self.client, "get_message"):
                try:
                    payload = await self.client.get_message(session_id, message_id)
                    info = _extract_info_from_message_payload(payload)
                    role = str(info.get("role") or "").lower()
                    info_message_id = str(info.get("id") or message_id or "")
                    if role and session_id and info_message_id:
                        _cache_put_limited(self._message_roles, (session_id, info_message_id), role, self._cache_limit)
                    for part in _extract_parts_from_message_payload(payload):
                        p_id = str(part.get("id") or "")
                        if session_id and info_message_id and p_id:
                            _cache_put_limited(self._part_meta, (session_id, info_message_id, p_id), _part_meta_from_part(part), self._cache_limit)
                except Exception:
                    pass
                if session_id and message_id:
                    message_role = self._message_roles.get((session_id, message_id))
                if session_id and message_id and part_id:
                    part_meta = self._part_meta.get((session_id, message_id, part_id))

        event = normalize_opencode_event(raw_event, session_store=self.session_store, task_store=self.task_store, settings=self.settings, tool_metadata=self.tool_metadata, message_role=message_role, part_meta=part_meta)
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
