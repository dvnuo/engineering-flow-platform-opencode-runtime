from __future__ import annotations

from typing import Any

from .opencode_run_state import ResolvedOpenCodeRunState
from .thinking_events import safe_preview, utc_now_iso


def run_state_diagnostics(resolved: ResolvedOpenCodeRunState | None) -> dict[str, Any]:
    if resolved is None:
        return {}
    return safe_preview(
        {
            "opencode_session_id": resolved.opencode_session_id,
            "opencode_status": resolved.status,
            "opencode_exists": resolved.exists,
            "opencode_active": resolved.active,
            "has_final_assistant": resolved.has_final_assistant,
            "child_sessions": list(resolved.child_sessions),
            "active_child_sessions": list(resolved.active_child_sessions),
            "reason": resolved.reason,
        },
        4000,
    )


def synthetic_active_run_from_resolved(
    *,
    portal_session_id: str,
    opencode_session_id: str,
    resolved: ResolvedOpenCodeRunState,
) -> dict[str, Any]:
    status = str(resolved.status or "busy")
    opencode_id = opencode_session_id or resolved.opencode_session_id
    return safe_preview(
        {
            "session_id": portal_session_id,
            "portal_session_id": portal_session_id,
            "opencode_session_id": opencode_id,
            "request_id": f"opencode-session-{opencode_id}",
            "status": status,
            "state": status,
            "source_of_truth": "opencode",
            "opencode_active": True,
            "opencode_status": status,
            "opencode_exists": bool(resolved.exists),
            "assistant_message_id": "",
            "assistant_message_ids": list(resolved.assistant_message_ids),
            "active_child_sessions": list(resolved.active_child_sessions),
            "can_abort": bool(resolved.exists),
            "action_hint": "wait_reconnect_or_stop",
            "reason": resolved.reason,
            "validation_reason": resolved.reason,
            "diagnostics": resolved.to_dict() or {},
        },
        12000,
    )


def active_run_public_from_store(
    active_run: dict[str, Any] | None,
    *,
    portal_session_id: str,
    opencode_session_id: str,
    resolved: ResolvedOpenCodeRunState,
) -> dict[str, Any] | None:
    if not isinstance(active_run, dict):
        return None
    public = dict(active_run)
    status = str(public.get("status") or resolved.status or "running")
    public["session_id"] = str(public.get("session_id") or portal_session_id)
    public["portal_session_id"] = str(public.get("portal_session_id") or portal_session_id)
    public["opencode_session_id"] = str(public.get("opencode_session_id") or opencode_session_id or resolved.opencode_session_id)
    public["status"] = status
    public["state"] = str(public.get("state") or public.get("completion_state") or status)
    public["source_of_truth"] = "opencode"
    public["opencode_active"] = True
    public["opencode_status"] = resolved.status
    public["opencode_exists"] = bool(resolved.exists)
    public["can_abort"] = bool(resolved.exists)
    public["action_hint"] = "wait_reconnect_or_stop"
    public["validation_reason"] = public.get("validation_reason") or resolved.reason
    public["reason"] = public.get("reason") or resolved.reason
    public["active_child_sessions"] = list(resolved.active_child_sessions)
    public["validated_at"] = public.get("validated_at") or utc_now_iso()
    diagnostics = public.get("diagnostics") if isinstance(public.get("diagnostics"), dict) else {}
    public["diagnostics"] = {
        **diagnostics,
        "opencode_status": resolved.status,
        "opencode_exists": bool(resolved.exists),
        "opencode_active": True,
        "child_sessions": list(resolved.child_sessions),
        "active_child_sessions": list(resolved.active_child_sessions),
    }
    return safe_preview(public, 12000)


def session_status_summary(
    resolved: ResolvedOpenCodeRunState,
    *,
    active_run: dict[str, Any] | None,
) -> dict[str, Any]:
    action_hint = "wait_reconnect_or_stop" if resolved.active else "safe_to_send"
    return {
        "status": {"type": resolved.status or "unknown"},
        "status_type": resolved.status or "unknown",
        "active": bool(resolved.active),
        "can_abort": bool(resolved.active and resolved.exists),
        "action_hint": action_hint,
        "reason": resolved.reason,
        "active_run": active_run,
        "active_child_sessions": list(resolved.active_child_sessions),
        "diagnostics": run_state_diagnostics(resolved),
    }
