from __future__ import annotations

import time
from typing import Any

from .chat_run_store import ChatRunRecord, ChatRunStore, TERMINAL_RUN_STATUSES
from .opencode_run_state import ResolvedOpenCodeRunState, resolve_opencode_run_state
from .thinking_events import safe_preview, utc_now_iso


def _validation_metadata(resolved: ResolvedOpenCodeRunState) -> dict[str, Any]:
    return {
        "validated_at": utc_now_iso(),
        "validation_reason": resolved.reason,
        "opencode_active": bool(resolved.active),
        "opencode_exists": bool(resolved.exists),
        "opencode_status": resolved.status,
        "opencode_source": resolved.source,
        "opencode_child_sessions": list(resolved.child_sessions),
        "opencode_active_child_sessions": list(resolved.active_child_sessions),
    }


async def _publish_validation_event(event_bus, event_type: str, *, record: ChatRunRecord, resolved: ResolvedOpenCodeRunState, state: str, data: dict[str, Any] | None = None) -> None:
    if event_bus is None or not hasattr(event_bus, "publish"):
        return
    event = {
        "type": event_type,
        "event_type": event_type,
        "engine": "opencode",
        "session_id": record.portal_session_id,
        "request_id": record.request_id,
        "opencode_session_id": record.opencode_session_id,
        "state": state,
        "summary": event_type,
        "data": safe_preview(
            {
                "validation_reason": resolved.reason,
                "opencode_active": resolved.active,
                "opencode_exists": resolved.exists,
                "opencode_status": resolved.status,
                **(data or {}),
            },
            2000,
        ),
        "created_at": utc_now_iso(),
        "ts": time.time(),
    }
    await event_bus.publish(event)


def _public_with_validation(store: ChatRunStore, record: ChatRunRecord | None, resolved: ResolvedOpenCodeRunState) -> dict[str, Any] | None:
    public = store.to_public_dict(record)
    if public is None:
        return None
    public["source_of_truth"] = "opencode"
    public["portal_session_id"] = getattr(record, "portal_session_id", "") if record is not None else str(public.get("session_id") or "")
    public["validated_at"] = public.get("validated_at") or utc_now_iso()
    public["validation_reason"] = resolved.reason
    public["opencode_active"] = bool(resolved.active)
    public["opencode_status"] = resolved.status
    public["opencode_exists"] = bool(resolved.exists)
    public["active_child_sessions"] = list(resolved.active_child_sessions)
    public["can_abort"] = bool(resolved.active and resolved.exists)
    public["action_hint"] = "wait_reconnect_or_stop" if resolved.active else "safe_to_send"
    diagnostics = public.get("diagnostics") if isinstance(public.get("diagnostics"), dict) else {}
    public["diagnostics"] = {
        **diagnostics,
        "opencode_status": resolved.status,
        "opencode_exists": bool(resolved.exists),
        "opencode_active": bool(resolved.active),
        "child_sessions": list(resolved.child_sessions),
        "active_child_sessions": list(resolved.active_child_sessions),
    }
    return safe_preview(public, 12000)


async def validate_chat_run_against_opencode(
    *,
    store: ChatRunStore,
    client,
    record: ChatRunRecord | None,
    event_bus=None,
) -> dict[str, Any] | None:
    if record is None:
        return None

    resolved = await resolve_opencode_run_state(client, record.opencode_session_id)
    metadata = _validation_metadata(resolved)

    if resolved.active:
        updated = store.record_validation(record.request_id, metadata) or record
        return _public_with_validation(store, updated, resolved)

    if not resolved.exists:
        stale = store.mark_stale(record.request_id, reason="opencode_session_missing", metadata=metadata)
        if stale is not None:
            await _publish_validation_event(event_bus, "chat.run.stale", record=stale, resolved=resolved, state="stale")
            await _publish_validation_event(event_bus, "opencode.session.missing", record=stale, resolved=resolved, state="missing")
        return None

    if resolved.has_final_assistant:
        completion = resolved.diagnostics.get("assistant_completion") if isinstance(resolved.diagnostics, dict) else {}
        text = str(completion.get("text") or "") if isinstance(completion, dict) else ""
        message_id = str(completion.get("message_id") or resolved.last_message_id or "") if isinstance(completion, dict) else resolved.last_message_id
        if record.status not in TERMINAL_RUN_STATUSES:
            updated = store.complete_run(
                record.request_id,
                {
                    "completion_state": "completed",
                    "response": text,
                    "assistant_message_id": message_id,
                    "assistant_message_ids": resolved.assistant_message_ids,
                    "validation_reason": resolved.reason,
                },
            )
        else:
            updated = record
        updated = store.record_validation(record.request_id, metadata) or updated or record
        return _public_with_validation(store, updated, resolved)

    stale_reason = "active_child_session_non_blocking" if resolved.reason == "active_child_session_non_blocking" else "opencode_not_active"
    stale = store.mark_stale(record.request_id, reason=stale_reason, metadata=metadata)
    if stale is not None:
        await _publish_validation_event(event_bus, "chat.run.stale", record=stale, resolved=resolved, state="stale")
    return None
