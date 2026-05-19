from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .opencode_client import is_session_missing_error
from .opencode_message_adapter import extract_assistant_message_ids, find_latest_assistant_completion, message_id
from .thinking_events import safe_preview


_ACTIVE_STATUS_VALUES = {
    "accepted",
    "active",
    "busy",
    "generating",
    "in_progress",
    "in-progress",
    "pending",
    "processing",
    "queued",
    "running",
    "streaming",
    "working",
}
_IDLE_OR_TERMINAL_STATUS_VALUES = {
    "aborted",
    "cancelled",
    "canceled",
    "complete",
    "completed",
    "done",
    "error",
    "errored",
    "failed",
    "idle",
    "ready",
    "stopped",
    "success",
    "unknown",
}


@dataclass
class ResolvedOpenCodeRunState:
    opencode_session_id: str
    exists: bool
    active: bool
    status: str
    source: str
    has_final_assistant: bool
    child_sessions: list[str] = field(default_factory=list)
    active_child_sessions: list[str] = field(default_factory=list)
    last_message_id: str = ""
    assistant_message_ids: list[str] = field(default_factory=list)
    reason: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return safe_preview(asdict(self), 8000)


def _normalize_status_value(status: Any) -> str:
    if isinstance(status, bool):
        return "active" if status else "idle"
    if isinstance(status, str):
        return status.strip().lower()
    if isinstance(status, dict):
        for key in ("status", "state", "phase", "run_state", "current_status"):
            value = status.get(key)
            if value is not None:
                return _normalize_status_value(value)
        for key in ("active", "running", "busy", "streaming", "pending"):
            if status.get(key) is True:
                return key
        for key in ("idle", "done", "completed", "complete"):
            if status.get(key) is True:
                return key
    return "unknown"


def is_opencode_status_active(status: Any) -> bool:
    normalized = _normalize_status_value(status)
    if normalized in _ACTIVE_STATUS_VALUES:
        return True
    return any(part in _ACTIVE_STATUS_VALUES for part in normalized.replace("-", "_").split())


def is_opencode_status_terminal_or_idle(status: Any) -> bool:
    normalized = _normalize_status_value(status)
    if normalized in _IDLE_OR_TERMINAL_STATUS_VALUES:
        return True
    return any(part in _IDLE_OR_TERMINAL_STATUS_VALUES for part in normalized.replace("-", "_").split())


def _extract_session_id(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("id", "session_id", "sessionID", "uuid"):
        item = value.get(key)
        if item:
            return str(item)
    for key in ("session", "info", "data"):
        nested = value.get(key)
        if isinstance(nested, dict):
            nested_id = _extract_session_id(nested)
            if nested_id:
                return nested_id
    return ""


def _find_status_entry(payload: Any, session_id: str) -> Any:
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            if _extract_session_id(item) == session_id:
                return item
        return None
    if not isinstance(payload, dict):
        return None
    if session_id in payload:
        return payload[session_id]
    if _extract_session_id(payload) == session_id:
        return payload
    for key in ("sessions", "data", "items", "statuses", "status"):
        value = payload.get(key)
        found = _find_status_entry(value, session_id)
        if found is not None:
            return found
    return None


def _child_session_ids(children: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    if not isinstance(children, list):
        return out
    for child in children:
        child_id = _extract_session_id(child)
        if child_id and child_id not in seen:
            seen.add(child_id)
            out.append(child_id)
    return out


async def resolve_opencode_run_state(client, opencode_session_id: str) -> ResolvedOpenCodeRunState:
    session_id = str(opencode_session_id or "").strip()
    if not session_id:
        return ResolvedOpenCodeRunState(
            opencode_session_id="",
            exists=False,
            active=False,
            status="missing",
            source="opencode",
            has_final_assistant=False,
            reason="missing_opencode_session_id",
            diagnostics={"missing_opencode_session_id": True},
        )

    status_payload: dict[str, Any] = {}
    status_entry: Any = None
    status = "unknown"
    status_active = False
    try:
        status_payload = await client.get_session_status()
        status_entry = _find_status_entry(status_payload, session_id)
        status = _normalize_status_value(status_entry)
        status_active = is_opencode_status_active(status_entry)
    except Exception as exc:
        if is_session_missing_error(exc):
            return ResolvedOpenCodeRunState(
                opencode_session_id=session_id,
                exists=False,
                active=False,
                status="missing",
                source="opencode",
                has_final_assistant=False,
                reason="opencode_session_missing",
                diagnostics={"status_error": safe_preview(str(exc), 1000), "opencode_status": getattr(exc, "status", None)},
            )
        raise

    try:
        messages = await client.list_messages(session_id)
    except Exception as exc:
        if is_session_missing_error(exc):
            return ResolvedOpenCodeRunState(
                opencode_session_id=session_id,
                exists=False,
                active=False,
                status="missing",
                source="opencode",
                has_final_assistant=False,
                reason="opencode_session_missing",
                diagnostics={"message_error": safe_preview(str(exc), 1000), "opencode_status": getattr(exc, "status", None)},
            )
        raise

    assistant_completion = find_latest_assistant_completion(messages)
    has_final_assistant = assistant_completion.get("completion_state") == "completed"
    assistant_message_ids = extract_assistant_message_ids(messages)
    last_message_id = message_id(messages[-1]) if messages else ""

    try:
        children = await client.list_session_children(session_id)
    except Exception as exc:
        if is_session_missing_error(exc):
            children = []
        else:
            raise
    child_sessions = _child_session_ids(children)
    active_child_sessions: list[str] = []
    for child_id in child_sessions:
        child_entry = _find_status_entry(status_payload, child_id)
        if child_entry is None:
            child_entry = next((child for child in children if isinstance(child, dict) and _extract_session_id(child) == child_id), None)
        if is_opencode_status_active(child_entry):
            active_child_sessions.append(child_id)

    active = bool(status_active or active_child_sessions)
    if active_child_sessions:
        reason = "active_child_session"
    elif status_active:
        reason = "opencode_status_active"
    elif has_final_assistant:
        reason = "final_assistant_message"
    elif status_entry is None:
        reason = "opencode_status_missing"
    elif is_opencode_status_terminal_or_idle(status_entry):
        reason = "opencode_not_active"
    else:
        reason = assistant_completion.get("reason") or "opencode_not_active"

    return ResolvedOpenCodeRunState(
        opencode_session_id=session_id,
        exists=True,
        active=active,
        status=status,
        source="opencode",
        has_final_assistant=has_final_assistant,
        child_sessions=child_sessions,
        active_child_sessions=active_child_sessions,
        last_message_id=last_message_id,
        assistant_message_ids=assistant_message_ids,
        reason=str(reason or ""),
        diagnostics={
            "status_entry": safe_preview(status_entry or {}, 2000),
            "status_active": status_active,
            "message_count": len(messages),
            "assistant_completion": safe_preview(assistant_completion, 2000),
            "child_count": len(child_sessions),
        },
    )
