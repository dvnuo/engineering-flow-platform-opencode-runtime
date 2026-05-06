from __future__ import annotations

from typing import Any

from .profile_store import sanitize_public_secrets
from .thinking_events import safe_preview


TRACE_KEYS = (
    "engine",
    "runtime_type",
    "agent_id",
    "request_id",
    "session_id",
    "task_id",
    "opencode_session_id",
    "tool_name",
    "tool_source",
    "skill_name",
    "profile_version",
    "runtime_profile_id",
    "group_id",
    "coordination_run_id",
    "model",
    "provider",
    "trace_id",
)


def _s(value: Any) -> str:
    sanitized = safe_preview(sanitize_public_secrets("" if value is None else str(value)), 300)
    return sanitized if isinstance(sanitized, str) else ""


def build_trace_context(settings, *, request_id: str = "", session_id: str = "", task_id: str = "", opencode_session_id: str = "", tool_name: str = "", tool_source: str = "", skill_name: str = "", profile_version: str = "", runtime_profile_id: str = "", group_id: str = "", coordination_run_id: str = "", model: str = "", provider: str = "") -> dict[str, str]:
    trace_id = request_id or task_id or session_id or opencode_session_id or ""
    out = {
        "engine": "opencode",
        "runtime_type": "opencode",
        "agent_id": _s(getattr(settings, "portal_agent_id", "") or ""),
        "request_id": _s(request_id),
        "session_id": _s(session_id),
        "task_id": _s(task_id),
        "opencode_session_id": _s(opencode_session_id),
        "tool_name": _s(tool_name),
        "tool_source": _s(tool_source),
        "skill_name": _s(skill_name),
        "profile_version": _s(profile_version),
        "runtime_profile_id": _s(runtime_profile_id),
        "group_id": _s(group_id),
        "coordination_run_id": _s(coordination_run_id),
        "model": _s(model),
        "provider": _s(provider),
        "trace_id": _s(trace_id),
    }
    return out


def add_trace_context(event: dict[str, Any], trace_context: dict[str, Any]) -> dict[str, Any]:
    tc = {k: _s(trace_context.get(k, "")) for k in TRACE_KEYS}
    event["trace_context"] = tc
    if not isinstance(event.get("data"), dict):
        event["data"] = {}
    event["data"]["trace_context"] = tc
    for key in ("agent_id", "group_id", "coordination_run_id", "runtime_type", "trace_id"):
        event[key] = tc.get(key, "")
    for key in ("request_id", "session_id", "task_id"):
        existing = event.get(key)
        if isinstance(existing, str) and existing:
            continue
        event[key] = tc.get(key, "")
    return event


def profile_version_from_metadata(metadata: dict[str, Any], runtime_profile: dict[str, Any]) -> tuple[str, str]:
    runtime_profile_id = metadata.get("runtime_profile_id") or runtime_profile.get("runtime_profile_id") or runtime_profile.get("id") or ""
    profile_version = metadata.get("runtime_profile_revision") or metadata.get("profile_revision") or runtime_profile.get("revision") or runtime_profile.get("version") or ""
    return _s(profile_version), _s(runtime_profile_id)
