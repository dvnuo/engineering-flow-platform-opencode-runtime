from __future__ import annotations

from typing import Any


_ACTIVE_STATUS_VALUES = {
    "busy",
    "running",
    "streaming",
    "pending",
    "processing",
    "working",
    "active",
    "in_progress",
    "in-progress",
    "generating",
    "queued",
}
_RETRY_STATUS_VALUES = {"retry", "retrying"}
_NEGATIVE_STATUS_VALUES = {
    "inactive",
    "not_active",
    "not_running",
    "not_busy",
    "not_streaming",
    "not_pending",
    "not_processing",
    "not_working",
    "not_retrying",
}
_IDLE_STATUS_VALUES = {
    "idle",
    "complete",
    "completed",
    "done",
    "stopped",
    "aborted",
    "cancelled",
    "canceled",
    "success",
    "ready",
}


def extract_session_id(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("id", "session_id", "sessionID", "sessionId", "uuid"):
        item = value.get(key)
        if item:
            return str(item)
    for key in ("session", "info", "data"):
        nested = value.get(key)
        if isinstance(nested, dict):
            nested_id = extract_session_id(nested)
            if nested_id:
                return nested_id
    return ""


def find_status_entry(payload: Any, session_id: str) -> Any:
    target = str(session_id or "")
    if not target:
        return None
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and extract_session_id(item) == target:
                return item
        return None
    if not isinstance(payload, dict):
        return None
    if target in payload:
        return payload[target]
    if extract_session_id(payload) == target:
        return payload
    for key in ("sessions", "data", "items", "statuses", "status"):
        found = find_status_entry(payload.get(key), target)
        if found is not None:
            return found
    return None


def _raw_status_value(status: Any) -> str:
    if status is None:
        return ""
    if isinstance(status, bool):
        return "busy" if status else "idle"
    if isinstance(status, str):
        return status.strip().lower()
    if isinstance(status, dict):
        for key in ("type", "status", "state", "phase", "run_state", "current_status"):
            value = status.get(key)
            if value is not None:
                return _raw_status_value(value)
        for key in ("active", "running", "busy", "streaming", "pending", "processing", "working"):
            if status.get(key) is True:
                return key
        for key in ("idle", "done", "completed", "complete", "aborted", "stopped"):
            if status.get(key) is True:
                return key
    return "unknown"


def normalize_status_type(status: Any, *, missing: bool = False, unreachable: bool = False) -> str:
    if unreachable:
        return "unknown"
    if missing:
        return "missing"
    raw = _raw_status_value(status)
    normalized = raw.replace("-", "_").strip().lower()
    if normalized in _NEGATIVE_STATUS_VALUES:
        return "idle"
    if normalized in _RETRY_STATUS_VALUES:
        return "retry"
    if normalized in _ACTIVE_STATUS_VALUES:
        return "busy"
    if normalized in _IDLE_STATUS_VALUES:
        return "idle"
    return "unknown"


def is_status_active(status: Any) -> bool:
    return normalize_status_type(status) in {"busy", "retry"}


def child_session_ids(children: Any) -> list[str]:
    items = children if isinstance(children, list) else []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        child_id = extract_session_id(item)
        if child_id and child_id not in seen:
            seen.add(child_id)
            out.append(child_id)
    return out


def build_conversation_status(
    raw_status: Any,
    opencode_session_id: str,
    *,
    children: list[dict[str, Any]] | None = None,
    unreachable: bool = False,
) -> dict[str, Any]:
    status_entry = None if unreachable else find_status_entry(raw_status, opencode_session_id)
    status_type = normalize_status_type(status_entry, missing=status_entry is None and not unreachable, unreachable=unreachable)
    active = status_type in {"busy", "retry"}
    can_send = (not active) and status_type != "unknown"
    can_abort = active
    if active:
        action_hint = "wait_or_stop"
    elif status_type == "unknown":
        action_hint = "refresh_status"
    else:
        action_hint = "safe_to_send"
    child_ids = child_session_ids(children or [])
    active_child_ids = [
        child_id
        for child_id in child_ids
        if is_status_active(find_status_entry(raw_status, child_id))
    ]
    return {
        "status": {
            "type": status_type,
            "active": active,
            "can_send": can_send,
            "can_abort": can_abort,
            "action_hint": action_hint,
        },
        "children": {
            "active_count": len(active_child_ids),
            "active_session_ids": active_child_ids,
            "non_blocking": True,
        },
        "status_entry": status_entry,
    }


def unreachable_status() -> dict[str, Any]:
    return {
        "status": {
            "type": "unknown",
            "active": False,
            "can_send": False,
            "can_abort": False,
            "action_hint": "refresh_status",
        },
        "children": {"active_count": 0, "active_session_ids": [], "non_blocking": True},
        "status_entry": None,
    }
