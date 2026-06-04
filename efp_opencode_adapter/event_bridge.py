from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from typing import Any

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

def _merge_properties_into_canonical(envelope: dict[str, Any]) -> dict[str, Any]:
    canonical = dict(envelope)
    props = canonical.get("properties")
    if isinstance(props, dict):
        for key, value in props.items():
            if key not in canonical:
                canonical[key] = value
    return canonical


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
        return _merge_properties_into_canonical(payload)
    data = raw_event.get("data")
    if isinstance(data, dict):
        return _merge_properties_into_canonical(data)
    return _merge_properties_into_canonical(raw_event)


def _event_type(raw_event: dict[str, Any], canonical: dict[str, Any]) -> str:
    explicit_event = canonical.get("event") or raw_event.get("event")
    if isinstance(explicit_event, str) and explicit_event.lower().startswith("session.status"):
        return "session.status"
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


def _role_candidates(values: dict[str, str], canonical: dict[str, Any], message_role: str | None, part_meta: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for value in (
        message_role,
        part_meta.get("role"),
        values.get("role"),
        values.get("message_role"),
        _first_string(values, "info.role"),
        canonical.get("role") if isinstance(canonical, dict) else "",
    ):
        if isinstance(value, str) and value.strip():
            out.append(value.strip().lower())
    return out


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
    return {}



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
_STANDARD_EVENT_TYPES = {
    "permission.requested",
    "permission.resolved",
}
_TERMINAL_SUCCESS_STATUSES = {"complete", "completed", "done", "end", "ended", "finish", "finished", "success", "succeeded", "ok"}
_TERMINAL_FAILED_STATUSES = {"blocked", "denied", "deny", "error", "failed", "failure", "rejected", "refused"}
_STARTED_STATUSES = {"", "call", "called", "created", "open", "pending", "running", "start", "started"}


def _is_standard_event_type(event_type: str) -> bool:
    return event_type.startswith("session.next.") or event_type in _STANDARD_EVENT_TYPES


def _safe_identifier(value: Any, *, fallback: str = "", max_chars: int = 180) -> str:
    raw = "" if value is None else str(value)
    if not raw:
        raw = fallback
    sanitized = _sanitize_event_text(raw, max_chars * 4)
    if not sanitized:
        sanitized = fallback
    if len(sanitized) <= max_chars:
        return sanitized
    digest = hashlib.sha256(sanitized.encode("utf-8", errors="ignore")).hexdigest()[:16]
    keep = max(8, max_chars - len(digest) - 1)
    return f"{sanitized[:keep]}-{digest}"


def _stable_generated_event_id(*, raw_event: dict[str, Any], event_type: str, raw_type: str, max_chars: int) -> str:
    sanitized = sanitize_public_secrets(raw_event)
    try:
        payload = json.dumps(sanitized, sort_keys=True, default=str, ensure_ascii=False)
    except Exception:
        payload = str(safe_preview(sanitized, max_chars))
    digest = hashlib.sha256(f"{event_type}\n{raw_type}\n{payload}".encode("utf-8", errors="ignore")).hexdigest()[:20]
    return f"opencode:{event_type}:{digest}"


def _raw_event_id(raw_event: dict[str, Any], canonical: dict[str, Any]) -> str:
    for container in (raw_event, canonical):
        for key in ("id", "event_id", "eventID", "opencode_event_id"):
            value = container.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _raw_properties(raw_event: dict[str, Any], canonical: dict[str, Any]) -> dict[str, Any]:
    for container in (canonical, raw_event):
        props = container.get("properties")
        if isinstance(props, dict):
            return dict(props)
    payload = raw_event.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("properties"), dict):
        return dict(payload["properties"])
    data = raw_event.get("data")
    if isinstance(data, dict):
        return dict(data)
    return {k: v for k, v in canonical.items() if k not in {"type", "event", "event_type", "opencode_event_id"}}

def _raw_session_id_from_event(raw_event: dict[str, Any], canonical: dict[str, Any], values: dict[str, str]) -> str:
    return _first_string(values, "opencode_session_id", "session_id", "sessionID", "sessionId")


def _opencode_session_id_from_values(values: dict[str, str]) -> str:
    return _first_string(values, "opencode_session_id", "sessionID", "sessionId", "session_id")


def _portal_session_id_from_values(values: dict[str, str], opencode_session_id: str, session_store) -> str:
    mapped = session_store.find_by_opencode_session_id(opencode_session_id) if opencode_session_id else None
    if mapped:
        return mapped.portal_session_id
    return _first_string(values, "portal_session_id", "session_id") or opencode_session_id


def _portal_request_id_from_values(values: dict[str, str]) -> str:
    return _first_string(values, "portal_request_id", "request_id")


def _raw_message_id_from_event(canonical: dict[str, Any], values: dict[str, str]) -> str:
    part = canonical.get("part") if isinstance(canonical.get("part"), dict) else {}
    for key in ("messageID", "messageId", "message_id"):
        value = part.get(key)
        if isinstance(value, str) and value:
            return value
    info = canonical.get("info") if isinstance(canonical.get("info"), dict) else {}
    value = info.get("id")
    if isinstance(value, str) and value:
        return value
    return _first_string(values, "messageID", "message_id", "messageId")


def _raw_part_id_from_event(canonical: dict[str, Any], values: dict[str, str]) -> str:
    part = canonical.get("part") if isinstance(canonical.get("part"), dict) else {}
    value = part.get("id")
    if isinstance(value, str) and value:
        return value
    return _first_string(values, "partID", "part_id", "partId")


def _call_id_from_event(canonical: dict[str, Any], values: dict[str, str]) -> str:
    part = canonical.get("part") if isinstance(canonical.get("part"), dict) else {}
    for key in ("callID", "callId", "call_id", "toolCallID", "toolCallId", "tool_call_id"):
        value = part.get(key)
        if isinstance(value, str) and value:
            return value
    return _first_string(values, "callID", "callId", "call_id", "toolCallID", "toolCallId", "tool_call_id")


def _step_status_event_type(part_type: str, status: str) -> str:
    if part_type in {"step-start", "step_start"}:
        return "session.next.step.started"
    if part_type in {"step-finish", "step_finish"}:
        if status in _TERMINAL_FAILED_STATUSES:
            return "session.next.step.failed"
        return "session.next.step.ended"
    return ""


def _text_status_event_type(part_type: str, status: str) -> str:
    if part_type not in {"text", "reasoning"}:
        return ""
    prefix = "session.next.reasoning" if part_type == "reasoning" else "session.next.text"
    if status in {"start", "started", "created", "open", "pending", "running"}:
        return f"{prefix}.started"
    if status in _TERMINAL_SUCCESS_STATUSES or status in _TERMINAL_FAILED_STATUSES:
        return f"{prefix}.ended"
    return ""


def _tool_event_type_from_status(status: str, legacy_type: str) -> str:
    if legacy_type == "tool.failed" or status in _TERMINAL_FAILED_STATUSES:
        return "session.next.tool.failed"
    if legacy_type == "tool.completed" or status in _TERMINAL_SUCCESS_STATUSES:
        return "session.next.tool.success"
    if legacy_type == "tool.started" or status in _STARTED_STATUSES:
        return "session.next.tool.called"
    return "session.next.tool.progress"


def _compaction_event_type(raw_type: str, status: str) -> str:
    if "compact" not in raw_type and "compaction" not in raw_type:
        return ""
    if "delta" in raw_type:
        return "session.next.compaction.delta"
    if status in _TERMINAL_SUCCESS_STATUSES or any(token in raw_type for token in ("end", "finish", "complete", "completed", "done")):
        return "session.next.compaction.ended"
    if status in _STARTED_STATUSES or any(token in raw_type for token in ("start", "begin", "started")):
        return "session.next.compaction.started"
    return "session.next.compaction.delta"


def _projected_type_for_event(*, raw_type: str, legacy_type: str, data: dict[str, Any], status: str, field: str) -> str:
    if _is_standard_event_type(raw_type):
        return raw_type
    compaction_type = _compaction_event_type(raw_type, status)
    if compaction_type:
        return compaction_type
    if legacy_type == "permission_request":
        return "permission.requested"
    if legacy_type == "permission_resolved":
        return "permission.resolved"
    if legacy_type in {"message.delta", "assistant_delta"}:
        return "session.next.text.delta"
    if legacy_type in {"llm_thinking", "opencode.reasoning"}:
        return "session.next.reasoning.delta"
    if legacy_type.startswith("tool."):
        return _tool_event_type_from_status(status, legacy_type)
    if legacy_type == "opencode.step.started":
        return "session.next.step.started"
    if legacy_type == "opencode.step.finished":
        return "session.next.step.failed" if status in _TERMINAL_FAILED_STATUSES else "session.next.step.ended"
    if legacy_type == "message.completed":
        return "session.next.step.ended"
    if raw_type == "message.part.delta":
        part_type = str(data.get("part_type") or "").lower()
        if part_type == "reasoning":
            return "session.next.reasoning.delta"
        if part_type == "tool" and field in {"input", "args", "arguments", "params"}:
            return "session.next.tool.input.delta"
    if raw_type == "message.part.updated":
        part_type = str(data.get("part_type") or "").lower()
        step_type = _step_status_event_type(part_type, status)
        if step_type:
            return step_type
        text_type = _text_status_event_type(part_type, status)
        if text_type:
            return text_type
    return legacy_type


def _state_for_projected_type(event_type: str, legacy_type: str) -> str:
    if event_type.endswith(".failed") or legacy_type.endswith(".failed"):
        return "failed"
    if event_type in {"permission.requested"}:
        return "pending"
    if event_type.endswith(".success") or event_type.endswith(".ended") or event_type == "permission.resolved":
        return "success"
    if event_type.endswith(".delta") or event_type.endswith(".progress") or event_type.endswith(".started") or event_type.endswith(".called"):
        return "running"
    return ""


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


def _standard_passthrough_event(
    raw_event: dict[str, Any],
    canonical: dict[str, Any],
    raw_type: str,
    values: dict[str, str],
    *,
    session_store,
    settings,
) -> dict[str, Any]:
    max_chars = settings.event_bridge_event_preview_chars
    raw_props = _raw_properties(raw_event, canonical)
    opencode_session_id = _opencode_session_id_from_values(values)
    session_id = _portal_session_id_from_values(values, opencode_session_id, session_store)
    request_id = _portal_request_id_from_values(values)
    event_id = _raw_event_id(raw_event, canonical)
    safe_event_id = _safe_identifier(
        event_id,
        fallback=_stable_generated_event_id(raw_event=raw_event, event_type=raw_type, raw_type=raw_type, max_chars=max_chars),
    )
    properties = _sanitize_event_value(raw_props, max_chars)
    if not isinstance(properties, dict):
        properties = {}
    created_at = canonical.get("created_at") or raw_event.get("created_at") or utc_now_iso()
    evt = {
        "id": safe_event_id,
        "type": raw_type,
        "event_type": raw_type,
        "engine": "opencode",
        "session_id": _sanitize_event_text(session_id, 300),
        "opencode_session_id": _sanitize_event_text(opencode_session_id, 300),
        "request_id": _sanitize_event_text(request_id, 300),
        "raw_type": _sanitize_event_text(raw_type, 200),
        "properties": properties,
        "data": dict(properties),
        "created_at": _sanitize_event_text(created_at, 100) or utc_now_iso(),
        "ts": time.time(),
        "state": _state_for_projected_type(raw_type, raw_type) or "received",
        "summary": raw_type,
        "metadata": {"raw_type": _sanitize_event_text(raw_type, 200), "projected": True},
    }
    message_id = _sanitize_event_text(_raw_message_id_from_event(canonical, values), 300)
    part_id = _sanitize_event_text(_raw_part_id_from_event(canonical, values), 300)
    call_id = _safe_identifier(_call_id_from_event(canonical, values), max_chars=120)
    if message_id:
        evt["opencode_message_id"] = message_id
        evt["data"]["message_id"] = evt["data"].get("message_id") or message_id
    if part_id:
        evt["opencode_part_id"] = part_id
        evt["data"]["part_id"] = evt["data"].get("part_id") or part_id
    if call_id:
        evt["call_id"] = call_id
        evt["data"]["call_id"] = evt["data"].get("call_id") or call_id
    return evt


def _apply_projected_protocol(
    event: dict[str, Any],
    *,
    raw_event: dict[str, Any],
    canonical: dict[str, Any],
    raw_type: str,
    legacy_type: str,
    values: dict[str, str],
    status: str,
    max_chars: int,
) -> dict[str, Any]:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    field = str(data.get("field") or values.get("__part_field") or values.get("field") or "").lower()
    projected_type = _projected_type_for_event(raw_type=raw_type, legacy_type=legacy_type, data=data, status=status, field=field)
    event_id = _raw_event_id(raw_event, canonical)
    event["id"] = _safe_identifier(
        event_id,
        fallback=_stable_generated_event_id(raw_event=raw_event, event_type=projected_type, raw_type=raw_type, max_chars=max_chars),
    )
    event["type"] = projected_type
    event["event_type"] = projected_type
    event["summary"] = projected_type if event.get("summary") == legacy_type else event.get("summary", projected_type)
    state = _state_for_projected_type(projected_type, legacy_type)
    if state:
        event["state"] = state
    if projected_type != legacy_type:
        event["legacy_type"] = legacy_type
        event["legacy_event_type"] = legacy_type
        data["legacy_type"] = legacy_type
        data["legacy_event_type"] = legacy_type
        data["compat_type"] = legacy_type
    data["event_type"] = projected_type
    data["raw_type"] = data.get("raw_type") or event.get("raw_type") or raw_type
    properties = _sanitize_event_value(_raw_properties(raw_event, canonical), max_chars)
    if isinstance(properties, dict):
        event["properties"] = properties
    call_id = _safe_identifier(_call_id_from_event(canonical, values), max_chars=120)
    if call_id:
        event["call_id"] = call_id
        data["call_id"] = data.get("call_id") or call_id
    event["data"] = data
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    metadata.update({"raw_type": event.get("raw_type", raw_type), "projected_type": projected_type})
    if projected_type != legacy_type:
        metadata["legacy_type"] = legacy_type
    event["metadata"] = metadata
    return event


def normalize_opencode_event(raw_event: dict[str, Any], *, session_store, task_store, settings, tool_metadata: dict[str, dict[str, Any]] | None = None, message_role: str | None = None, part_meta: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if not isinstance(raw_event, dict):
        return None
    max_chars = settings.event_bridge_event_preview_chars
    canonical = _canonical(raw_event)
    raw_type = _event_type(raw_event, canonical)
    values: dict[str, str] = {}
    _collect_strings(raw_event, values)
    _collect_strings(canonical, values)
    if _is_standard_event_type(raw_type):
        return _standard_passthrough_event(raw_event, canonical, raw_type, values, session_store=session_store, settings=settings)

    opencode_session_id = _opencode_session_id_from_values(values)
    permission_id = _first_string(values, "permissionID", "permission_id", "requestID", "request_id", "id")
    portal_request_id = _first_string(values, "portal_request_id")
    opencode_request_id = _first_string(values, "requestID", "request_id", "id")
    tool = _tool_name(values)
    input_preview = _first_string(values, "input", "arguments", "args", "params", "command")
    output_preview = _first_string(values, "output", "result", "response")
    text = _message_text(canonical, values)
    risk_level = _first_string(values, "risk_level", "riskLevel") or "medium"
    status = _status_value(values)

    message_ids = {values.get(k, "") for k in ("message_id", "messageID", "messageId", "parentID", "parent_id") if values.get(k)}
    mapped = session_store.find_by_opencode_session_id(opencode_session_id) if opencode_session_id else None
    session_id = mapped.portal_session_id if mapped else (opencode_session_id or "")
    normalized_type = "opencode.sync" if raw_type == "sync" else "opencode.raw"

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
            if status in {"failed", "error", "rejected", "denied"}:
                normalized_type = "tool.failed"
            else:
                normalized_type = "tool.started" if status in {"", "started", "running", "pending"} else "tool.completed"
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
        normalized_type = "opencode.raw"
        text = ""
        canonical_part = canonical.get("part") if isinstance(canonical.get("part"), dict) else {}
        pmeta = part_meta if isinstance(part_meta, dict) and part_meta else (_part_meta_from_part(canonical_part) if canonical_part else {})
        role_values = _role_candidates(values, canonical, message_role, pmeta)
        role = next((r for r in role_values if r in {"assistant", "user"}), (role_values[0] if role_values else ""))
        ptype = str(pmeta.get("type") or "").lower()
        ignored = _bool_true(pmeta.get("ignored"))
        synthetic = _bool_true(pmeta.get("synthetic"))
        metadata_incomplete = not role or not ptype
        role_explicit_user = "user" in role_values
        if field == "text" and delta and not ignored and not synthetic and not role_explicit_user:
            if ptype == "reasoning":
                normalized_type = "llm_thinking"
                text = delta
            elif ptype in {"", "text"} and role in {"", "assistant"}:
                normalized_type = "message.delta"
                text = delta
        values["__part_field"] = field
        values["__part_message_id"] = message_id
        values["__part_id"] = part_id
        values["__message_role"] = role
        values["__part_type"] = ptype
        values["__metadata_incomplete"] = "true" if metadata_incomplete else "false"
    elif raw_type in {"message.completed", "message.finished"}:
        normalized_type = "message.completed"
    elif raw_type.startswith("session."):
        normalized_type = "session.updated"
    retry_status = canonical.get("status") if isinstance(canonical.get("status"), dict) else {}
    if raw_type == "session.status" and isinstance(retry_status, dict) and str(retry_status.get("type", "")).lower() == "retry":
        normalized_type = "provider.retry"
    if (
        any(token in raw_type for token in ("provider.retry", "model.retry", "rate.limit", "rate_limit", "ratelimit"))
        or status in {"retry", "retrying", "rate_limit", "rate_limited", "ratelimited"}
    ):
        normalized_type = "provider.retry"

    task_id = _map_task_id(task_store, opencode_session_id, message_ids)
    s_permission_id = _sanitize_event_value(permission_id, 300)
    s_tool = _sanitize_event_value(tool, 300)
    s_input = _sanitize_event_value(input_preview, max_chars)
    s_output = _sanitize_event_value(output_preview, max_chars)
    s_risk = _sanitize_event_value(risk_level, 100)
    s_text = _sanitize_event_value(text, max_chars)
    s_status = _sanitize_event_value(status, 100)
    s_session_id = _sanitize_event_text(session_id, 300)
    s_opencode_session_id = _sanitize_event_text(opencode_session_id, 300)
    s_request_id = _sanitize_event_text(portal_request_id, 300)
    s_raw_request_id = _sanitize_event_text(opencode_request_id, 300)
    s_raw_type = _sanitize_event_text(raw_type, 200)

    data = {
        "raw_event_preview": _sanitize_event_value(raw_event, max_chars),
        "canonical_preview": _sanitize_event_value(canonical, max_chars),
        "raw_type": s_raw_type,
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
    data["metadata_incomplete"] = values.get("__metadata_incomplete") == "true"
    data["message_id"] = _sanitize_event_text(values.get("__part_message_id") or _raw_message_id_from_event(canonical, values), 300)
    data["part_id"] = _sanitize_event_text(values.get("__part_id") or _raw_part_id_from_event(canonical, values), 300)

    if raw_type == "message.updated":
        info = canonical.get("info") if isinstance(canonical.get("info"), dict) else {}
        data["raw_type"] = "message.updated"
        data["info"] = _sanitize_event_value(info, max_chars)
        data["message_id"] = _sanitize_event_text(str(info.get("id") or data.get("message_id") or ""), 300)
    elif raw_type == "message.part.updated":
        part = canonical.get("part") if isinstance(canonical.get("part"), dict) else {}
        part_message_id = str(part.get("messageID") or part.get("messageId") or part.get("message_id") or data.get("message_id") or "")
        part_id = str(part.get("id") or data.get("part_id") or "")
        part_type = str(part.get("type") or data.get("part_type") or "")
        data["raw_type"] = "message.part.updated"
        data["part"] = _sanitize_event_value(part, max_chars)
        data["part_id"] = _sanitize_event_text(part_id, 300)
        data["part_type"] = _sanitize_event_text(part_type, 100)
        data["message_id"] = _sanitize_event_text(part_message_id, 300)
    elif raw_type == "message.part.delta":
        delta = canonical.get("delta") if isinstance(canonical.get("delta"), str) else values.get("delta", "")
        pmeta = part_meta if isinstance(part_meta, dict) and part_meta else {}
        canonical_part = canonical.get("part") if isinstance(canonical.get("part"), dict) else {}
        if not pmeta and canonical_part:
            pmeta = _part_meta_from_part(canonical_part)
        data["raw_type"] = "message.part.delta"
        if isinstance(delta, str) and delta:
            data["delta"] = _sanitize_event_text(delta, max_chars)
        data["field"] = _sanitize_event_text(str(canonical.get("field") or values.get("field") or data.get("field") or ""), 100)
        data["part_id"] = _sanitize_event_text(data.get("part_id") or _raw_part_id_from_event(canonical, values), 300)
        data["message_id"] = _sanitize_event_text(data.get("message_id") or _raw_message_id_from_event(canonical, values), 300)
        data["part_type"] = _sanitize_event_text(str(data.get("part_type") or pmeta.get("type") or ""), 100)
    elif raw_type == "session.status":
        status_obj = canonical.get("status") if isinstance(canonical.get("status"), dict) else {}
        status_type = ""
        if status_obj:
            status_type = str(status_obj.get("type") or status_obj.get("status") or "")
        else:
            for key in ("status_type", "type", "status", "state"):
                value = canonical.get(key)
                if isinstance(value, str) and value:
                    if key == "type" and value == raw_type:
                        continue
                    status_type = value
                    break
        data["raw_type"] = "session.status"
        if status_obj:
            data["status"] = _sanitize_event_value(status_obj, max_chars)
        else:
            data["status"] = _sanitize_event_value({"type": status_type or "unknown"}, max_chars)
        data["status_type"] = _sanitize_event_text(status_type or "unknown", 100)
    elif raw_type == "session.idle":
        data["raw_type"] = "session.idle"
        data["reconcile_hint"] = "fetch_session_messages"

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
        "metadata": {"raw_type": s_raw_type},
        "created_at": utc_now_iso(),
        "ts": time.time(),
    }
    if data.get("message_id"):
        evt["opencode_message_id"] = data["message_id"]
    if data.get("part_id"):
        evt["opencode_part_id"] = data["part_id"]
    if data.get("status_type"):
        evt["opencode_status"] = data["status_type"]
    if s_raw_request_id:
        evt["raw_request_id"] = s_raw_request_id
        evt["opencode_request_id"] = s_raw_request_id
        evt["data"]["raw_request_id"] = s_raw_request_id
        evt["data"]["opencode_request_id"] = s_raw_request_id
    if normalized_type == "provider.retry":
        retry_data = retry_status if isinstance(retry_status, dict) else {}
        evt["state"] = "retrying"
        evt["summary"] = "Provider API retry"
        evt["data"]["message"] = _sanitize_event_value(retry_data.get("message") or output_preview or text or status, max_chars)
        evt["data"]["attempt"] = retry_data.get("attempt")
        evt["data"]["next"] = retry_data.get("next")
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
            tool_source = meta.get("source_ref") or "opencode"
        elif str(tool).lower() in _BUILTIN_TOOLS:
            tool_source = "opencode_builtin"
        else:
            tool_source = "unknown"
        evt["tool_name"] = s_tool
    else:
        meta = (tool_metadata or {}).get(str(tool)) or (tool_metadata or {}).get(str(s_tool))
        if isinstance(meta, dict):
            tool_source = meta.get("source_ref") or "opencode"
        elif str(tool).lower() in _BUILTIN_TOOLS:
            tool_source = "opencode_builtin"
        else:
            tool_source = "unknown"
    trace_context = build_trace_context(settings, request_id=s_request_id, session_id=s_session_id, task_id=task_id or "", opencode_session_id=s_opencode_session_id, tool_name=s_tool if (normalized_type.startswith("tool.") or normalized_type.startswith("permission_")) else "", tool_source=tool_source)
    add_trace_context(evt, trace_context)
    evt["tool_source"] = _sanitize_event_text(tool_source, 200)
    return _apply_projected_protocol(
        evt,
        raw_event=raw_event,
        canonical=canonical,
        raw_type=raw_type,
        legacy_type=normalized_type,
        values=values,
        status=status,
        max_chars=max_chars,
    )


class OpenCodeEventBridge:
    def __init__(self, settings, client, event_bus, session_store, task_store, chatlog_store=None, request_binding_store=None):
        self.settings = settings
        self.client = client
        self.event_bus = event_bus
        self.session_store = session_store
        self.task_store = task_store
        self.chatlog_store = chatlog_store
        self.request_binding_store = request_binding_store
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

    async def _publish_lifecycle_event(self, event_type: str, *, error: str = "") -> None:
        payload = {
            "type": event_type,
            "event_type": event_type,
            "engine": "opencode",
            "state": "connected" if event_type in {"event_bridge.connected", "event_bridge.reconnected"} else "disconnected",
            "summary": event_type,
            "created_at": utc_now_iso(),
            "ts": time.time(),
            "reconnects": self.reconnects,
        }
        if error:
            payload["error"] = error
            payload["data"] = {"error": error, "reconnects": self.reconnects}
        else:
            payload["data"] = {"reconnects": self.reconnects}
        await self.event_bus.publish(payload)

    async def publish_raw_event(self, raw_event: dict) -> dict | None:
        canonical = _canonical(raw_event)
        raw_type = _event_type(raw_event, canonical)
        values: dict[str, str] = {}
        _collect_strings(raw_event, values)
        _collect_strings(canonical, values)
        session_id = _opencode_session_id_from_values(values)
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
        if self.request_binding_store is not None and session_id:
            task_id = str(event.get("task_id") or "")
            binding = None
            if (message_id or task_id) and hasattr(self.request_binding_store, "resolve_exact"):
                binding = self.request_binding_store.resolve_exact(opencode_session_id=session_id, message_id=message_id, task_id=task_id)
            if binding is None:
                binding = self.request_binding_store.resolve(opencode_session_id=session_id, message_id=message_id, task_id=task_id)
            if binding is not None:
                event["session_id"] = binding.portal_session_id
                event["request_id"] = binding.request_id
                event["portal_request_id"] = binding.request_id
                event["opencode_session_id"] = binding.opencode_session_id
                data = event.get("data") if isinstance(event.get("data"), dict) else {}
                data["request_id"] = binding.request_id
                data["portal_request_id"] = binding.request_id
                data["opencode_session_id"] = binding.opencode_session_id
                if binding.task_id:
                    data["task_id"] = binding.task_id
                event["data"] = data
                properties = event.get("properties") if isinstance(event.get("properties"), dict) else {}
                properties["request_id"] = binding.request_id
                properties["portal_request_id"] = binding.request_id
                properties["opencode_session_id"] = binding.opencode_session_id
                event["properties"] = properties
                trace_context = event.get("trace_context") if isinstance(event.get("trace_context"), dict) else {}
                trace_context["request_id"] = binding.request_id
                trace_context["session_id"] = binding.portal_session_id
                trace_context["opencode_session_id"] = binding.opencode_session_id
                trace_context["trace_id"] = binding.request_id or trace_context.get("trace_id", "")
                event["trace_context"] = trace_context
                if isinstance(event.get("data"), dict) and isinstance(event["data"].get("trace_context"), dict):
                    event["data"]["trace_context"].update(
                        {
                            "request_id": binding.request_id,
                            "session_id": binding.portal_session_id,
                            "opencode_session_id": binding.opencode_session_id,
                            "trace_id": binding.request_id or event["data"]["trace_context"].get("trace_id", ""),
                        }
                    )
                if binding.task_id and not event.get("task_id"):
                    event["task_id"] = binding.task_id
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
                    await self._publish_lifecycle_event("event_bridge.reconnected" if self.reconnects else "event_bridge.connected")
                    async for raw in self.client.event_stream(global_events=True, timeout_seconds=None):
                        await self.publish_raw_event(raw)
                    self.connected = False
                    await self._publish_lifecycle_event("event_bridge.disconnected")
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
                    await self._publish_lifecycle_event("event_bridge.disconnected", error=self.last_error)
                    self.reconnects += 1
                    await asyncio.sleep(backoff)
                    backoff = min(self.settings.event_bridge_max_backoff_seconds, backoff * 2)
        finally:
            self.running = False
            self.connected = False
