from __future__ import annotations

import json
from typing import Any, AsyncIterator

from aiohttp import web

from .opencode_status_adapter import normalize_status_type
from .thinking_events import safe_preview


def event_session_id(event: Any) -> str:
    if not isinstance(event, dict):
        return ""
    for key in ("session_id", "sessionID", "sessionId", "opencode_session_id"):
        if event.get(key):
            return str(event[key])
    for key in ("data", "session", "message", "info", "properties"):
        nested = event.get(key)
        if isinstance(nested, dict):
            nested_id = event_session_id(nested)
            if nested_id:
                return nested_id
    return ""


def event_matches_session(event: dict[str, Any], opencode_session_id: str) -> bool:
    session_id = event_session_id(event)
    return not session_id or session_id == opencode_session_id


def normalize_opencode_event(
    event: dict[str, Any],
    *,
    conversation_id: str,
    opencode_session_id: str,
) -> tuple[str, dict[str, Any]]:
    raw_type = str(event.get("type") or event.get("event_type") or "message").strip()
    raw_lower = raw_type.lower()
    lowered = raw_type.lower().replace("_", ".").replace("-", ".")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}

    if lowered in {"server.connected", "connected"}:
        name = "opencode.connected"
    elif "permission" in lowered:
        name = "opencode.permission.requested"
    elif "snapshot" in lowered:
        name = "opencode.snapshot.required"
    elif "error" in lowered:
        name = "opencode.error"
    elif "part" in lowered and "delta" in lowered:
        name = "opencode.message.part.delta"
    elif "part" in lowered:
        name = "opencode.message.part.updated"
    elif "message" in lowered:
        name = "opencode.message.updated"
    elif "session" in lowered or "status" in lowered:
        name = "opencode.session.status"
    else:
        name = "opencode.message.updated"

    payload: dict[str, Any] = {
        "conversation_id": conversation_id,
        "opencode_session_id": opencode_session_id,
        "opencode_event_type": raw_type,
        "data": safe_preview(data, 4000),
        "raw": safe_preview(event, 4000),
    }

    if lowered in {"session.idle", "session.idle.event"} or raw_lower == "session.idle":
        payload.update(
            {
                "status": "idle",
                "active": False,
                "can_abort": False,
                "can_send": True,
                "action_hint": "safe_to_send",
                "snapshot_required": True,
            }
        )
        name = "opencode.session.status"

    status_value = data.get("status") or data.get("state") or event.get("status") or event.get("state")
    if name == "opencode.session.status" and status_value is not None:
        status_type = normalize_status_type(status_value)
        active = status_type in {"busy", "retry"}
        payload.update(
            {
                "status": status_type,
                "active": active,
                "can_abort": active,
                "can_send": status_type == "idle",
                "action_hint": "wait_or_stop" if active else ("safe_to_send" if status_type == "idle" else "refresh_status"),
            }
        )

    for source_key, target_keys in {
        "messageID": ("messageID", "message_id", "messageId"),
        "message_id": ("messageID", "message_id", "messageId"),
        "messageId": ("messageID", "message_id", "messageId"),
        "partID": ("partID", "part_id", "partId"),
        "part_id": ("partID", "part_id", "partId"),
        "partId": ("partID", "part_id", "partId"),
        "permissionID": ("permissionID", "permission_id", "permissionId"),
        "permission_id": ("permissionID", "permission_id", "permissionId"),
        "permissionId": ("permissionID", "permission_id", "permissionId"),
    }.items():
        value = event.get(source_key) or data.get(source_key)
        if value:
            for target_key in target_keys:
                payload[target_key] = str(value)

    for key in ("message", "part", "permission"):
        value = data.get(key) if isinstance(data.get(key), dict) else event.get(key)
        if isinstance(value, dict):
            payload[key] = safe_preview(value, 4000)
    return name, payload


async def iter_filtered_events(
    events: AsyncIterator[dict[str, Any]],
    *,
    conversation_id: str,
    opencode_session_id: str,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    async for event in events:
        if not isinstance(event, dict):
            continue
        if not event_matches_session(event, opencode_session_id):
            continue
        yield normalize_opencode_event(
            event,
            conversation_id=conversation_id,
            opencode_session_id=opencode_session_id,
        )


async def write_sse_response(
    request: web.Request,
    events: AsyncIterator[dict[str, Any]],
    *,
    conversation_id: str,
    opencode_session_id: str,
) -> web.StreamResponse:
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    await response.prepare(request)
    async for event_name, payload in iter_filtered_events(
        events,
        conversation_id=conversation_id,
        opencode_session_id=opencode_session_id,
    ):
        line = f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        await response.write(line.encode("utf-8"))
    await response.write_eof()
    return response
