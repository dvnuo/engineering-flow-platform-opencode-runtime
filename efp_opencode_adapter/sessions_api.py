from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import uuid4

from aiohttp import web
from .app_keys import (
    CHAT_RUN_STORE_KEY,
    CHATLOG_STORE_KEY,
    EVENT_BUS_KEY,
    OPENCODE_CLIENT_KEY,
    PORTAL_METADATA_CLIENT_KEY,
    SESSION_STORE_KEY,
    TASK_BACKGROUND_TASKS_KEY,
    USER_DISPLAY_STORE_KEY,
)

from .chat_api import handle_chat_payload_for_app
from .chat_run_validation import validate_chat_run_against_opencode
from .opencode_client import OpenCodeClientError
from .opencode_ids import new_opencode_message_id
from .opencode_message_adapter import message_to_visible_text, to_efp_message
from .thinking_events import safe_preview, utc_now_iso


logger = logging.getLogger(__name__)


def _json_bad_request(error: str) -> web.HTTPBadRequest:
    return web.HTTPBadRequest(text=json.dumps({"error": error}), content_type="application/json")


def _json_not_found(error: str, **extra) -> web.HTTPNotFound:
    payload = {"error": error, **extra}
    return web.HTTPNotFound(text=json.dumps(payload), content_type="application/json")


def _json_bad_gateway(error: str, **extra) -> web.HTTPBadGateway:
    payload = {"error": error, **extra}
    return web.HTTPBadGateway(text=json.dumps(payload), content_type="application/json")


def _json_conflict(error: str, **extra) -> web.HTTPConflict:
    payload = {"error": error, **extra}
    return web.HTTPConflict(text=json.dumps(payload), content_type="application/json")


def _opencode_detail(exc: OpenCodeClientError) -> str:
    return str(exc)


def _unexpected_upstream_detail(exc: Exception) -> str:
    return str(exc) or exc.__class__.__name__


async def _delete_portal_metadata_best_effort(portal_metadata, session_id: str) -> dict[str, Any]:
    if not portal_metadata:
        return {"success": False, "skipped": True, "reason": "portal_metadata_not_available"}
    try:
        return await portal_metadata.delete_session_metadata(session_id)
    except Exception as exc:
        return {"success": False, "error": safe_preview(str(exc), 1000)}




def _delete_chatlog_best_effort(chatlog_store, session_id: str) -> dict[str, Any]:
    if not chatlog_store or not hasattr(chatlog_store, "delete"):
        return {"success": False, "skipped": True, "reason": "chatlog_delete_not_supported"}
    try:
        deleted = chatlog_store.delete(session_id)
        return {"success": True, "deleted": bool(deleted)}
    except Exception as exc:
        return {"success": False, "error": safe_preview(str(exc), 1000)}


def _delete_user_display_best_effort(display_store, *, portal_session_id: str | None = None, opencode_session_id: str | None = None) -> dict[str, Any]:
    if not display_store or not hasattr(display_store, "delete_session"):
        return {"success": False, "skipped": True, "reason": "display_store_delete_not_supported"}
    try:
        deleted_count = display_store.delete_session(portal_session_id=portal_session_id, opencode_session_id=opencode_session_id)
        return {"success": True, "deleted_count": int(deleted_count or 0)}
    except Exception as exc:
        return {"success": False, "error": safe_preview(str(exc), 1000)}


async def _read_json_object(request: web.Request, *, error_prefix: str = "payload") -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise _json_bad_request("invalid_json")
    if not isinstance(body, dict):
        raise _json_bad_request(f"{error_prefix}_must_be_object")
    return body


def _message_info(message: Any) -> dict[str, Any]:
    if isinstance(message, dict) and isinstance(message.get("info"), dict):
        return message["info"]
    if isinstance(message, dict):
        return message
    return {}


def message_to_text(message: Any) -> str:
    return message_to_visible_text(message)


def _message_parts(message: Any) -> list[dict[str, Any]]:
    if isinstance(message, dict):
        parts = message.get("parts")
        if isinstance(parts, list):
            return [p for p in parts if isinstance(p, dict)]
        message_obj = message.get("message")
        if isinstance(message_obj, dict) and isinstance(message_obj.get("parts"), list):
            return [p for p in message_obj["parts"] if isinstance(p, dict)]
    return []


def normalize_canonical_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in messages:
        if not isinstance(raw, dict):
            continue

        info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
        parts = raw.get("parts") if isinstance(raw.get("parts"), list) else []

        if not info and isinstance(raw.get("message"), dict):
            msg = raw["message"]
            info = msg.get("info") if isinstance(msg.get("info"), dict) else {
                k: v for k, v in msg.items() if k != "parts"
            }
            if not parts and isinstance(msg.get("parts"), list):
                parts = msg["parts"]

        message_id = str(info.get("id") or raw.get("id") or raw.get("message_id") or "")
        role = str(info.get("role") or raw.get("role") or "")

        out.append(
            {
                "info": safe_preview(info, 12000),
                "parts": safe_preview([p for p in parts if isinstance(p, dict)], 50000),
                "message_id": message_id,
                "role": role,
                "source_of_truth": "opencode",
            }
        )
    return out


def _is_internal_efp_message(message: Any) -> bool:
    mid = _message_id(message)
    if mid.startswith("efp-auto-continue-"):
        return True
    for part in _message_parts(message):
        metadata = part.get("metadata")
        if isinstance(metadata, dict) and metadata.get("efp_internal") == "auto_continue":
            return True
    return False


def _to_efp_messages(
    messages: list[dict[str, Any]],
    *,
    display_store=None,
    portal_session_id: str = "",
    opencode_session_id: str = "",
) -> list[dict[str, Any]]:
    filtered = [msg for msg in messages if not _is_internal_efp_message(msg)]
    out = []
    for idx, msg in enumerate(filtered):
        efp = to_efp_message(msg, index=idx)
        if str(efp.get("role") or "").lower() == "user" and display_store is not None:
            metadata = efp.get("metadata") if isinstance(efp.get("metadata"), dict) else {}
            message_id = str(efp.get("id") or metadata.get("opencode_message_id") or "")
            display = display_store.get_user_message(
                opencode_session_id=opencode_session_id or str(metadata.get("opencode_session_id") or ""),
                opencode_message_id=message_id,
                portal_session_id=portal_session_id,
            )
            if display:
                visible = str(display.get("display_content") or "")
                efp["content"] = visible
                efp["display_content"] = visible
                display_attachments = display.get("display_attachments")
                efp["attachments"] = display_attachments if isinstance(display_attachments, list) else []
                metadata = efp.setdefault("metadata", {})
                metadata["display_content_source"] = "portal_original_user_message"
                metadata["internal_model_content_hidden"] = True
                metadata["original_user_message"] = visible
        out.append(efp)
    return out


def _normalize_visible_content(content: str) -> str:
    return (content or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _visible_message_signatures(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {"role": _message_role(message), "content": _normalize_visible_content(message_to_text(message))}
        for message in messages
        if not _is_internal_efp_message(message)
    ]


def _prefix_matches(expected_messages: list[dict[str, Any]], actual_messages: list[dict[str, Any]]) -> tuple[bool, int, int]:
    expected = _visible_message_signatures(expected_messages)
    actual = _visible_message_signatures(actual_messages)
    return expected == actual, len(expected), len(actual)


def _message_id(message: Any, fallback: str = "") -> str:
    info = _message_info(message)
    return str(info.get("id") or (message.get("id") if isinstance(message, dict) else "") or (message.get("message_id") if isinstance(message, dict) else "") or fallback)


def _message_role(message: Any) -> str:
    info = _message_info(message)
    raw = info.get("role") or (message.get("role") if isinstance(message, dict) else "")
    return str(raw or "").lower()


def _find_message_index(messages: list[dict[str, Any]], message_id: str) -> int:
    for idx, message in enumerate(messages):
        if _message_id(message) == message_id:
            return idx
    return -1


def _last_message_text(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return ""
    return message_to_text(messages[-1])


def _extract_opencode_session_id(payload: dict[str, Any] | Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("id", "session_id", "sessionID", "uuid"):
        value = payload.get(key)
        if value:
            return str(value)
    for nested_key in ("session", "data", "info"):
        nested_value = payload.get(nested_key)
        nested_id = _extract_opencode_session_id(nested_value)
        if nested_id:
            return nested_id
    return ""


def _fork_boundary_candidates(messages: list[dict[str, Any]], target_idx: int) -> list[dict[str, str]]:
    seen: set[str] = set()
    candidates: list[dict[str, str]] = []

    def add_message(idx: int) -> None:
        if idx < 0 or idx >= len(messages):
            return
        message = messages[idx]
        message_id = _message_id(message)
        if not message_id or message_id in seen:
            return
        seen.add(message_id)
        candidates.append({"message_id": message_id, "role": _message_role(message)})

    add_message(target_idx)
    add_message(target_idx - 1)
    for idx in range(target_idx - 1, -1, -1):
        if _message_role(messages[idx]) == "user":
            add_message(idx)
            break

    return candidates


async def _fork_candidate_with_abort_retry(client, old_opencode_session_id: str, boundary_message_id: str) -> dict[str, Any]:
    try:
        return await client.fork_session(old_opencode_session_id, boundary_message_id)
    except OpenCodeClientError as exc:
        if exc.status not in {409, 423}:
            raise
        await client.abort_session(old_opencode_session_id)
        return await client.fork_session(old_opencode_session_id, boundary_message_id)


async def _fork_session_preserving_prefix(
    client,
    old_opencode_session_id: str,
    messages: list[dict[str, Any]],
    target_idx: int,
    record,
    allow_revert_fallback: bool = False,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    target_message = messages[target_idx]
    target_message_id = _message_id(target_message)
    expected_prefix_messages = messages[:target_idx]
    expected_prefix_count = len(_visible_message_signatures(expected_prefix_messages))
    attempted_boundaries: list[dict[str, Any]] = []
    last_actual_prefix_count = 0

    base_metadata = {
        "old_opencode_session_id": old_opencode_session_id,
        "deleted_from_message_id": target_message_id,
        "target_message_id": target_message_id,
        "expected_prefix_count": expected_prefix_count,
        "prefix_validated": False,
        "allow_revert_fallback_requested": bool(allow_revert_fallback),
        "revert_fallback_disabled": True,
        "attempted_boundaries": attempted_boundaries,
    }

    if target_idx == 0:
        attempt = {"message_id": "", "role": "session_start", "result": "fork_failed"}
        attempted_boundaries.append(attempt)
        try:
            created = await client.create_session(title=record.title)
            new_opencode_session_id = _extract_opencode_session_id(created)
            if not new_opencode_session_id:
                attempt["error"] = "missing_fork_session_id"
                raise _json_bad_gateway("opencode_mutation_failed", detail="missing_fork_session_id")
            new_messages = await client.list_messages(new_opencode_session_id)
        except OpenCodeClientError as exc:
            attempt["status"] = exc.status
            attempt["error"] = safe_preview(_opencode_detail(exc), 1000)
            raise _json_bad_gateway("opencode_mutation_failed", detail=_opencode_detail(exc))
        except web.HTTPException:
            raise
        except Exception as exc:
            attempt["error"] = safe_preview(_unexpected_upstream_detail(exc), 1000)
            raise _json_bad_gateway("opencode_mutation_failed", detail=_unexpected_upstream_detail(exc))

        valid, _, actual_prefix_count = _prefix_matches(expected_prefix_messages, new_messages)
        last_actual_prefix_count = actual_prefix_count
        attempt["actual_prefix_count"] = actual_prefix_count
        if valid:
            attempt["result"] = "matched"
            metadata = {
                **base_metadata,
                "strategy": "new_empty_session",
                "opencode_session_id": new_opencode_session_id,
                "accepted_boundary_message_id": "",
                "accepted_boundary_role": "session_start",
                "actual_prefix_count": actual_prefix_count,
                "prefix_validated": True,
            }
            return new_opencode_session_id, new_messages, metadata

        attempt["result"] = "prefix_mismatch"
        metadata = {
            **base_metadata,
            "strategy": "prefix_validation_failed",
            "opencode_session_id": new_opencode_session_id,
            "accepted_boundary_message_id": "",
            "accepted_boundary_role": "",
            "actual_prefix_count": last_actual_prefix_count,
        }
        raise _json_conflict(
            "opencode_fork_prefix_mismatch",
            expected_prefix_count=expected_prefix_count,
            actual_prefix_count=last_actual_prefix_count,
            detail="OpenCode mutation session did not match the expected prefix",
            metadata=metadata,
        )

    for candidate in _fork_boundary_candidates(messages, target_idx):
        boundary_message_id = candidate["message_id"]
        boundary_role = candidate["role"]
        attempt = {"message_id": boundary_message_id, "role": boundary_role, "result": "fork_failed"}
        attempted_boundaries.append(attempt)

        try:
            forked = await _fork_candidate_with_abort_retry(client, old_opencode_session_id, boundary_message_id)
            new_opencode_session_id = _extract_opencode_session_id(forked)
            if not new_opencode_session_id:
                attempt["error"] = "missing_fork_session_id"
                continue
        except OpenCodeClientError as exc:
            attempt["status"] = exc.status
            attempt["error"] = safe_preview(_opencode_detail(exc), 1000)
            continue
        except Exception as exc:
            attempt["error"] = safe_preview(_unexpected_upstream_detail(exc), 1000)
            continue

        try:
            new_messages = await client.list_messages(new_opencode_session_id)
        except OpenCodeClientError as exc:
            attempt["result"] = "list_failed"
            attempt["status"] = exc.status
            attempt["error"] = safe_preview(_opencode_detail(exc), 1000)
            continue
        except Exception as exc:
            attempt["result"] = "list_failed"
            attempt["error"] = safe_preview(_unexpected_upstream_detail(exc), 1000)
            continue

        valid, _, actual_prefix_count = _prefix_matches(expected_prefix_messages, new_messages)
        last_actual_prefix_count = actual_prefix_count
        attempt["actual_prefix_count"] = actual_prefix_count
        if not valid:
            attempt["result"] = "prefix_mismatch"
            continue

        attempt["result"] = "matched"
        metadata = {
            **base_metadata,
            "strategy": "fork_before_target",
            "opencode_session_id": new_opencode_session_id,
            "accepted_boundary_message_id": boundary_message_id,
            "accepted_boundary_role": boundary_role,
            "actual_prefix_count": actual_prefix_count,
            "prefix_validated": True,
        }
        return new_opencode_session_id, new_messages, metadata

    metadata = {
        **base_metadata,
        "strategy": "prefix_validation_failed",
        "opencode_session_id": "",
        "accepted_boundary_message_id": "",
        "accepted_boundary_role": "",
        "actual_prefix_count": last_actual_prefix_count,
    }
    raise _json_conflict(
        "opencode_fork_prefix_mismatch",
        expected_prefix_count=expected_prefix_count,
        actual_prefix_count=last_actual_prefix_count,
        detail="OpenCode fork did not preserve the expected message prefix",
        metadata=metadata,
    )


async def list_sessions_handler(request: web.Request) -> web.Response:
    store = request.app[SESSION_STORE_KEY]
    records = sorted(store.list_active(), key=lambda x: x.updated_at, reverse=True)
    return web.json_response(
        {
            "sessions": [
                {
                    "session_id": r.portal_session_id,
                    "name": r.title,
                    "last_message": r.last_message,
                    "updated_at": r.updated_at,
                    "message_count": r.message_count,
                    "engine": "opencode",
                }
                for r in records
            ]
        }
    )


_FAILED_CHATLOG_STATUSES = {"failed", "error"}
_FAILED_EVENT_TYPES = {"edit.failed", "chat.failed", "execution.failed", "error"}


def _event_type(event: Any) -> str:
    if not isinstance(event, dict):
        return ""
    return str(event.get("event_type") or event.get("type") or "")


def _latest_runtime_event(runtime_events: Any) -> dict[str, Any]:
    if not isinstance(runtime_events, list):
        return {}
    for event in reversed(runtime_events):
        if isinstance(event, dict):
            return event
    return {}


def _event_error(event: dict[str, Any]) -> str:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    value = data.get("error") if isinstance(data, dict) else ""
    return safe_preview(str(value), 1000) if value else ""


def _latest_entry_error(latest: dict[str, Any], latest_event: dict[str, Any]) -> str:
    context_state = latest.get("context_state") if isinstance(latest.get("context_state"), dict) else {}
    llm_debug = latest.get("llm_debug") if isinstance(latest.get("llm_debug"), dict) else {}
    for value in (
        _event_error(latest_event),
        context_state.get("summary"),
        llm_debug.get("error"),
        latest.get("error"),
        latest.get("response"),
        "regeneration failed",
    ):
        if isinstance(value, str) and value.strip():
            return safe_preview(value.strip(), 1000)
    return "regeneration failed"


def _latest_session_runtime_status_metadata(chatlog_store, sid: str) -> dict[str, Any]:
    if not chatlog_store or not hasattr(chatlog_store, "latest_entry"):
        return {}
    try:
        latest = chatlog_store.latest_entry(sid)
    except Exception:
        return {}
    if not isinstance(latest, dict):
        return {}

    status = str(latest.get("status") or "")
    request_id = str(latest.get("request_id") or "")
    latest_event = _latest_runtime_event(latest.get("runtime_events"))
    latest_event_type = _event_type(latest_event)
    failed = status.lower() in _FAILED_CHATLOG_STATUSES or latest_event_type in _FAILED_EVENT_TYPES
    timestamp = str(latest.get("finished_at") or latest.get("created_at") or "")

    if not failed:
        out: dict[str, Any] = {}
        if status:
            out["chatlog_status"] = safe_preview(status, 100)
        if request_id:
            out["request_id"] = safe_preview(request_id, 200)
        if timestamp:
            out["latest_chatlog_at"] = safe_preview(timestamp, 100)
        return out

    error = _latest_entry_error(latest, latest_event)
    event_type = latest_event_type if latest_event_type in _FAILED_EVENT_TYPES else "chat.failed"
    return {
        "latest_event_type": safe_preview(event_type, 100),
        "latest_event_state": "error",
        "completion_state": "error",
        "request_id": safe_preview(request_id, 200),
        "incomplete_reason": error,
        "error": error,
        "runtime_events": [safe_preview(latest_event, 1000)] if latest_event else [],
        "chatlog_status": "failed",
        "latest_chatlog_at": safe_preview(timestamp, 100) if timestamp else "",
    }


async def _chat_run_session_metadata(chat_run_store: Any, client: Any, sid: str, messages: list[dict[str, Any]], event_bus: Any = None) -> dict[str, Any]:
    if chat_run_store is None:
        return {"active_run": None, "latest_run": None}
    try:
        active_record = chat_run_store.active_for_session(sid)
        latest_record = chat_run_store.latest_for_session(sid)
    except Exception:
        return {"active_run": None, "latest_run": None}
    active_public = None
    active_run_stale_reason = ""
    if active_record is not None:
        active_public = await validate_chat_run_against_opencode(store=chat_run_store, client=client, record=active_record, event_bus=event_bus)
        if active_public is None or active_public.get("opencode_active") is not True:
            refreshed = chat_run_store.get(active_record.request_id) if hasattr(chat_run_store, "get") else active_record
            active_run_stale_reason = str(
                (active_public or {}).get("validation_reason")
                or getattr(refreshed, "incomplete_reason", "")
                or "opencode_not_active"
            )
            active_record = None
        else:
            active_record = chat_run_store.get(active_record.request_id) if hasattr(chat_run_store, "get") else active_record
    latest_record = chat_run_store.latest_for_session(sid)
    active_run = chat_run_store.to_session_summary(active_record) if active_record is not None else None
    latest_run = chat_run_store.to_session_summary(latest_record) if latest_record is not None else None
    metadata: dict[str, Any] = {"active_run": active_run, "latest_run": latest_run}
    if not active_run_stale_reason and active_record is None and latest_record is not None and getattr(latest_record, "status", "") == "stale":
        active_run_stale_reason = str(getattr(latest_record, "incomplete_reason", "") or latest_record.metadata.get("validation_reason") or "opencode_not_active")
    if active_run_stale_reason:
        metadata["active_run_stale_reason"] = active_run_stale_reason
    if latest_record is not None and latest_record.last_response_text:
        message_ids = {str(message.get("id") or (message.get("metadata") or {}).get("opencode_message_id") or "") for message in messages if isinstance(message, dict)}
        assistant_ids = [latest_record.assistant_message_id, *latest_record.assistant_message_ids]
        has_matching_message = any(message_id and message_id in message_ids for message_id in assistant_ids)
        if not has_matching_message:
            metadata["assistant_projection"] = {
                "request_id": latest_record.request_id,
                "assistant_message_id": latest_record.assistant_message_id,
                "text": latest_record.last_response_text,
                "display_blocks": list(latest_record.last_display_blocks),
            }
    return metadata


async def get_session_handler(request: web.Request) -> web.Response:
    sid = request.match_info["session_id"]
    store = request.app[SESSION_STORE_KEY]
    client = request.app[OPENCODE_CLIENT_KEY]
    record = store.get(sid)
    if not record or record.deleted:
        raise web.HTTPNotFound(text=json.dumps({"error": "session_not_found"}), content_type="application/json")
    try:
        messages = await client.list_messages(record.opencode_session_id)
    except OpenCodeClientError as exc:
        raise web.HTTPBadGateway(text=json.dumps({"error": "opencode_error", "detail": str(exc)}), content_type="application/json")
    efp_messages = _to_efp_messages(
        messages,
        display_store=request.app.get(USER_DISPLAY_STORE_KEY),
        portal_session_id=sid,
        opencode_session_id=record.opencode_session_id,
    )
    canonical_messages = normalize_canonical_messages(messages)
    return web.json_response(
        {
            "success": True,
            "engine": "opencode",
            "source_of_truth": "opencode",
            "session_id": sid,
            "opencode_session_id": record.opencode_session_id,
            "name": record.title,
            "messages": efp_messages,
            "canonical_messages": canonical_messages,
            "metadata": {
                "engine": "opencode",
                "source_of_truth": "opencode",
                "opencode_session_id": record.opencode_session_id,
                "partial_recovery": record.partial_recovery,
                **_latest_session_runtime_status_metadata(request.app.get(CHATLOG_STORE_KEY), sid),
                **await _chat_run_session_metadata(request.app.get(CHAT_RUN_STORE_KEY), client, sid, efp_messages, request.app.get(EVENT_BUS_KEY)),
            },
        }
    )


async def session_chatlog_handler(request: web.Request) -> web.Response:
    sid = request.match_info["session_id"]
    chatlog_store = request.app[CHATLOG_STORE_KEY]
    try:
        chatlog = chatlog_store.get(sid)
    except Exception:
        return web.json_response({"success": True, "session_id": sid, "chatlog": None, "events": [], "runtime_events": [], "context_state": {}, "llm_debug": {}, "metadata": {"engine": "opencode", "corrupted_chatlog": True}, "status": "unknown", "request_id": ""})
    if not chatlog:
        return web.json_response({"success": True, "session_id": sid, "chatlog": None, "events": [], "runtime_events": [], "context_state": {}, "llm_debug": {}, "metadata": {"engine": "opencode"}, "status": "unknown", "request_id": ""})
    latest = chatlog_store.latest_entry(sid) or {}
    return web.json_response({"success": True, "session_id": sid, "chatlog": chatlog, "events": latest.get("events", []), "runtime_events": latest.get("runtime_events", []), "context_state": latest.get("context_state", {}), "llm_debug": latest.get("llm_debug", {}), "metadata": {"engine": "opencode", "status": latest.get("status", "unknown"), "request_id": latest.get("request_id", "")}, "status": latest.get("status", "unknown"), "request_id": latest.get("request_id", ""), "timestamp": latest.get("finished_at") or latest.get("created_at")})


async def rename_session_handler(request: web.Request) -> web.Response:
    sid = request.match_info["session_id"]
    store = request.app[SESSION_STORE_KEY]
    client = request.app[OPENCODE_CLIENT_KEY]
    record = store.get(sid)
    if not record or record.deleted:
        raise web.HTTPNotFound(text=json.dumps({"error": "session_not_found"}), content_type="application/json")
    body = await _read_json_object(request, error_prefix="rename_payload")
    raw_title = body.get("name") if body.get("name") is not None else body.get("title")
    if not isinstance(raw_title, str):
        raise _json_bad_request("title_required")
    title = raw_title.strip()
    if not title:
        raise _json_bad_request("title_required")
    updated = store.rename(sid, title)
    try:
        await client.patch_session(updated.opencode_session_id, title)
    except OpenCodeClientError as exc:
        if exc.status != 404:
            raise web.HTTPBadGateway(text=json.dumps({"error": "opencode_error", "detail": str(exc)}), content_type="application/json")
    return web.json_response({"success": True, "session_id": sid, "name": title})


async def delete_session_handler(request: web.Request) -> web.Response:
    sid = request.match_info["session_id"]
    store = request.app[SESSION_STORE_KEY]
    client = request.app[OPENCODE_CLIENT_KEY]
    portal_metadata = request.app.get(PORTAL_METADATA_CLIENT_KEY)
    chatlog_store = request.app.get(CHATLOG_STORE_KEY)
    chat_run_store = request.app.get(CHAT_RUN_STORE_KEY)
    display_store = request.app.get(USER_DISPLAY_STORE_KEY)
    record = store.get(sid)
    if record is None or record.deleted:
        metadata_delete = await _delete_portal_metadata_best_effort(portal_metadata, sid)
        chat_runs_deleted = chat_run_store.delete_for_session(sid) if chat_run_store is not None and hasattr(chat_run_store, "delete_for_session") else 0
        display_delete = _delete_user_display_best_effort(
            display_store,
            portal_session_id=sid,
            opencode_session_id=record.opencode_session_id if record else None,
        )
        return web.json_response({"success": True, "session_id": sid, "already_deleted": True, "runtime_deleted": False, "opencode_deleted": False, "opencode_missing": record is None, "metadata_delete": metadata_delete, "display_delete": display_delete, "chat_runs_deleted": chat_runs_deleted})
    abort_result = None
    if chat_run_store is not None and hasattr(chat_run_store, "active_for_session"):
        active_run = chat_run_store.active_for_session(sid)
        if active_run is not None and getattr(active_run, "opencode_session_id", "") and hasattr(client, "abort_session_tree"):
            try:
                abort_result = await client.abort_session_tree(active_run.opencode_session_id)
            except Exception as exc:
                abort_result = {"success": False, "error": safe_preview(str(exc), 1000)}
    opencode_deleted = False
    opencode_missing = False
    try:
        await client.delete_session(record.opencode_session_id)
        opencode_deleted = True
    except OpenCodeClientError as exc:
        if exc.status == 404:
            opencode_missing = True
        else:
            return web.json_response({"success": False, "error": "opencode_delete_failed", "session_id": sid, "opencode_session_id": record.opencode_session_id, "opencode_status": exc.status, "detail": str(exc)}, status=502)
    store.mark_deleted(sid)
    chat_runs_deleted = chat_run_store.delete_for_session(sid) if chat_run_store is not None and hasattr(chat_run_store, "delete_for_session") else 0
    chatlog_delete = _delete_chatlog_best_effort(chatlog_store, sid)
    display_delete = _delete_user_display_best_effort(display_store, portal_session_id=sid, opencode_session_id=record.opencode_session_id)
    metadata_delete = await _delete_portal_metadata_best_effort(portal_metadata, sid)
    return web.json_response({"success": True, "session_id": sid, "opencode_session_id": record.opencode_session_id, "already_deleted": False, "runtime_deleted": True, "opencode_deleted": opencode_deleted, "opencode_missing": opencode_missing, "metadata_delete": metadata_delete, "chatlog_delete": chatlog_delete, "display_delete": display_delete, "chat_runs_deleted": chat_runs_deleted, "abort_result": abort_result})


async def clear_sessions_handler(request: web.Request) -> web.Response:
    store = request.app[SESSION_STORE_KEY]
    client = request.app[OPENCODE_CLIENT_KEY]
    portal_metadata = request.app.get(PORTAL_METADATA_CLIENT_KEY)
    chatlog_store = request.app.get(CHATLOG_STORE_KEY)
    chat_run_store = request.app.get(CHAT_RUN_STORE_KEY)
    display_store = request.app.get(USER_DISPLAY_STORE_KEY)
    failures = []
    deleted_count = 0
    metadata_results = []
    for rec in store.list_active():
        abort_result = None
        if chat_run_store is not None and hasattr(chat_run_store, "active_for_session"):
            active_run = chat_run_store.active_for_session(rec.portal_session_id)
            if active_run is not None and getattr(active_run, "opencode_session_id", "") and hasattr(client, "abort_session_tree"):
                try:
                    abort_result = await client.abort_session_tree(active_run.opencode_session_id)
                except Exception as exc:
                    abort_result = {"success": False, "error": safe_preview(str(exc), 1000)}
        try:
            await client.delete_session(rec.opencode_session_id)
            op_missing = False
        except OpenCodeClientError as exc:
            if exc.status == 404:
                op_missing = True
            else:
                failures.append({"session_id": rec.portal_session_id, "opencode_session_id": rec.opencode_session_id, "status": exc.status, "detail": str(exc)})
                continue
        store.mark_deleted(rec.portal_session_id)
        deleted_count += 1
        chat_runs_deleted = chat_run_store.delete_for_session(rec.portal_session_id) if chat_run_store is not None and hasattr(chat_run_store, "delete_for_session") else 0
        chatlog_delete = _delete_chatlog_best_effort(chatlog_store, rec.portal_session_id)
        display_delete = _delete_user_display_best_effort(display_store, portal_session_id=rec.portal_session_id, opencode_session_id=rec.opencode_session_id)
        metadata = await _delete_portal_metadata_best_effort(portal_metadata, rec.portal_session_id)
        metadata_results.append({"session_id": rec.portal_session_id, "opencode_missing": op_missing, "metadata_delete": metadata, "chatlog_delete": chatlog_delete, "display_delete": display_delete, "chat_runs_deleted": chat_runs_deleted, "abort_result": abort_result})
    if failures:
        return web.json_response({"success": False, "deleted_count": deleted_count, "failed_count": len(failures), "failures": failures, "metadata_delete": metadata_results}, status=502)
    return web.json_response({"success": True, "deleted_count": deleted_count, "failed_count": 0, "metadata_delete": metadata_results})


async def _delete_from_here(*, store, client, portal_session_id: str, message_id: str, allow_revert_fallback: bool = False):
    record = store.get(portal_session_id)
    if not record or record.deleted:
        raise _json_not_found("session_not_found")
    old_opencode_session_id = record.opencode_session_id
    try:
        messages = await client.list_messages(old_opencode_session_id)
    except OpenCodeClientError as exc:
        if exc.status == 404:
            raise _json_not_found("opencode_session_not_found", detail=_opencode_detail(exc))
        raise _json_bad_gateway("opencode_mutation_failed", detail=_opencode_detail(exc))
    except Exception as exc:
        raise _json_bad_gateway("opencode_mutation_failed", detail=_unexpected_upstream_detail(exc))
    idx = _find_message_index(messages, message_id)
    if idx < 0:
        raise _json_not_found("message_not_found")
    new_opencode_session_id, new_messages, metadata = await _fork_session_preserving_prefix(
        client,
        old_opencode_session_id,
        messages,
        idx,
        record,
        allow_revert_fallback=allow_revert_fallback,
    )
    updated_record = store.replace_opencode_session_after_mutation(
        portal_session_id,
        new_opencode_session_id,
        message_count=len(_visible_message_signatures(new_messages)),
        last_message=_last_message_text(new_messages),
    )
    return updated_record, new_messages, metadata


async def delete_message_from_here_handler(request: web.Request) -> web.Response:
    sid = request.match_info["session_id"]
    mid = request.match_info["message_id"]
    allow_revert_fallback = False
    if request.can_read_body:
        try:
            body = await request.json()
            if isinstance(body, dict):
                allow_revert_fallback = bool(body.get("allow_revert_fallback"))
        except Exception:
            allow_revert_fallback = False
    updated_record, new_messages, metadata = await _delete_from_here(store=request.app[SESSION_STORE_KEY], client=request.app[OPENCODE_CLIENT_KEY], portal_session_id=sid, message_id=mid, allow_revert_fallback=allow_revert_fallback)
    return web.json_response({
        "success": True,
        "session_id": sid,
        "message_id": mid,
        "engine": "opencode",
        "mutation": "delete_from_here",
        "messages": _to_efp_messages(
            new_messages,
            display_store=request.app.get(USER_DISPLAY_STORE_KEY),
            portal_session_id=sid,
            opencode_session_id=updated_record.opencode_session_id,
        ),
        "metadata": metadata,
    })


def _edit_content_from_body(body: dict[str, Any]) -> str:
    for key in ("content", "new_content", "message"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _request_id_from_edit_body(body: dict[str, Any]) -> str:
    value = body.get("request_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return str(uuid4())


def _optional_body_str(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _merged_async_edit_metadata(body: dict[str, Any], *, message_id: str, replacement_user_message_id: str) -> dict[str, Any]:
    metadata = body.get("metadata")
    out = dict(metadata) if isinstance(metadata, dict) else {}
    out.update(
        {
            "edit": True,
            "edited_message_id": message_id,
            "replacement_user_message_id": replacement_user_message_id,
            "source": "message_edit_async",
        }
    )
    return out


def _chatlog_runtime_events(chatlog_store, session_id: str, request_id: str) -> list[dict[str, Any]]:
    if not chatlog_store or not hasattr(chatlog_store, "get"):
        return []
    try:
        chatlog = chatlog_store.get(session_id)
    except Exception:
        return []
    if not isinstance(chatlog, dict):
        return []
    entries = chatlog.get("entries")
    if not isinstance(entries, list):
        return []
    for entry in reversed(entries):
        if isinstance(entry, dict) and entry.get("request_id") == request_id:
            events = entry.get("runtime_events")
            return list(events) if isinstance(events, list) else []
    return []


async def _record_async_edit_failure(
    app: web.Application,
    *,
    session_id: str,
    request_id: str,
    opencode_session_id: str,
    edited_message_id: str,
    replacement_user_message_id: str,
    error: str,
) -> None:
    event = {
        "type": "edit.failed",
        "event_type": "edit.failed",
        "state": "error",
        "ok": False,
        "completion_state": "error",
        "session_id": session_id,
        "request_id": request_id,
        "opencode_session_id": opencode_session_id,
        "created_at": utc_now_iso(),
        "data": {
            "error": safe_preview(error, 1000),
            "edited_message_id": edited_message_id,
            "replacement_user_message_id": replacement_user_message_id,
            "source": "message_edit_async",
        },
    }
    bus = app.get(EVENT_BUS_KEY)
    if bus is not None and hasattr(bus, "publish"):
        try:
            await bus.publish(event)
        except Exception:
            logger.warning("failed to publish async edit failure event", exc_info=True)

    chatlog_store = app.get(CHATLOG_STORE_KEY)
    if chatlog_store is not None and hasattr(chatlog_store, "fail_entry"):
        runtime_events = [*_chatlog_runtime_events(chatlog_store, session_id, request_id), event]
        try:
            chatlog_store.fail_entry(
                session_id,
                request_id=request_id,
                error=error,
                runtime_events=runtime_events,
                context_state={
                    "objective": "Async edit resend",
                    "summary": safe_preview(error, 300),
                    "current_state": "error",
                    "next_step": "Retry edit after resolving the runtime error",
                },
                llm_debug={
                    "engine": "opencode",
                    "opencode_session_id": opencode_session_id,
                    "edited_message_id": edited_message_id,
                    "replacement_user_message_id": replacement_user_message_id,
                    "source": "message_edit_async",
                },
            )
        except Exception:
            logger.warning("failed to record async edit failure in chatlog", exc_info=True)

    portal_metadata = app.get(PORTAL_METADATA_CLIENT_KEY)
    if portal_metadata is not None and hasattr(portal_metadata, "publish_session_metadata"):
        try:
            await portal_metadata.publish_session_metadata(
                session_id=session_id,
                latest_event_type="edit.failed",
                latest_event_state="error",
                request_id=request_id,
                summary=safe_preview(error, 300),
                runtime_events=[event],
                metadata={
                    "engine": "opencode",
                    "opencode_session_id": opencode_session_id,
                    "edited_message_id": edited_message_id,
                    "replacement_user_message_id": replacement_user_message_id,
                    "source": "message_edit_async",
                },
            )
        except Exception:
            logger.warning("failed to publish async edit failure metadata", exc_info=True)


async def _run_async_edit_resend(
    app: web.Application,
    *,
    payload: dict[str, Any],
    session_id: str,
    request_id: str,
    opencode_session_id: str,
    edited_message_id: str,
    replacement_user_message_id: str,
) -> None:
    try:
        await handle_chat_payload_for_app(app, payload)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error = getattr(exc, "text", None) or str(exc) or exc.__class__.__name__
        logger.exception(
            "Async edit background resend failed",
            extra={"session_id": session_id, "request_id": request_id, "edited_message_id": edited_message_id},
        )
        await _record_async_edit_failure(
            app,
            session_id=session_id,
            request_id=request_id,
            opencode_session_id=opencode_session_id,
            edited_message_id=edited_message_id,
            replacement_user_message_id=replacement_user_message_id,
            error=error,
        )


def _track_background_task(app: web.Application, task: asyncio.Task) -> None:
    task_set = app.get(TASK_BACKGROUND_TASKS_KEY)
    if isinstance(task_set, set):
        task_set.add(task)
        task.add_done_callback(task_set.discard)


async def _edit_message_async_from_body(request: web.Request, body: dict[str, Any]) -> web.Response:
    sid = request.match_info["session_id"]
    mid = request.match_info["message_id"]
    content = _edit_content_from_body(body)
    if not content:
        raise _json_bad_request("content_required")

    store = request.app[SESSION_STORE_KEY]
    client = request.app[OPENCODE_CLIENT_KEY]
    record = store.get(sid)
    if not record or record.deleted:
        raise _json_not_found("session_not_found")
    try:
        old_messages = await client.list_messages(record.opencode_session_id)
    except OpenCodeClientError as exc:
        if exc.status == 404:
            raise _json_not_found("opencode_session_not_found", detail=_opencode_detail(exc))
        raise _json_bad_gateway("opencode_edit_failed", detail=_opencode_detail(exc))
    except Exception as exc:
        raise _json_bad_gateway("opencode_edit_failed", detail=_unexpected_upstream_detail(exc))

    idx = _find_message_index(old_messages, mid)
    if idx < 0:
        raise _json_not_found("message_not_found")
    if _message_role(old_messages[idx]) != "user":
        raise _json_bad_request("only_user_message_edit_supported")

    updated_record, new_messages, metadata = await _delete_from_here(
        store=store,
        client=client,
        portal_session_id=sid,
        message_id=mid,
        allow_revert_fallback=bool(body.get("allow_revert_fallback", False)),
    )
    request_id = _request_id_from_edit_body(body)
    replacement_user_message_id = new_opencode_message_id()
    payload: dict[str, Any] = {
        "message": content,
        "session_id": sid,
        "request_id": request_id,
        "message_id": replacement_user_message_id,
        "metadata": _merged_async_edit_metadata(
            body,
            message_id=mid,
            replacement_user_message_id=replacement_user_message_id,
        ),
    }
    model = _optional_body_str(body, "model")
    agent = _optional_body_str(body, "agent")
    system = _optional_body_str(body, "system")
    if model:
        payload["model_override"] = model
    if agent:
        payload["agent"] = agent
    if system:
        payload["system"] = system

    task: asyncio.Task | None = None
    try:
        task = asyncio.create_task(
            _run_async_edit_resend(
                request.app,
                payload=payload,
                session_id=sid,
                request_id=request_id,
                opencode_session_id=updated_record.opencode_session_id,
                edited_message_id=mid,
                replacement_user_message_id=replacement_user_message_id,
            )
        )
        _track_background_task(request.app, task)
    except Exception as exc:
        if task is not None:
            task.cancel()
        raise _json_bad_gateway("edit_async_background_start_failed", detail=_unexpected_upstream_detail(exc), metadata=metadata)

    return web.json_response(
        {
            "success": True,
            "accepted": True,
            "async": True,
            "completion_state": "pending",
            "session_id": sid,
            "message_id": mid,
            "replacement_user_message_id": replacement_user_message_id,
            "assistant_message_id": "",
            "request_id": request_id,
            "response": "",
            "messages": _to_efp_messages(
                new_messages,
                display_store=request.app.get(USER_DISPLAY_STORE_KEY),
                portal_session_id=sid,
                opencode_session_id=updated_record.opencode_session_id,
            ),
            "metadata": {**metadata, "edit_async": True, "background_started": True},
        },
        status=202,
    )


async def edit_message_async_handler(request: web.Request) -> web.Response:
    body = await _read_json_object(request)
    return await _edit_message_async_from_body(request, body)


async def edit_message_handler(request: web.Request) -> web.Response:
    sid = request.match_info["session_id"]
    mid = request.match_info["message_id"]
    body = await _read_json_object(request)
    if body.get("async") is True:
        return await _edit_message_async_from_body(request, body)
    content = _edit_content_from_body(body)
    if not content:
        raise _json_bad_request("content_required")
    store = request.app[SESSION_STORE_KEY]
    client = request.app[OPENCODE_CLIENT_KEY]
    record = store.get(sid)
    if not record or record.deleted:
        raise _json_not_found("session_not_found")
    try:
        old_messages = await client.list_messages(record.opencode_session_id)
    except OpenCodeClientError as exc:
        if exc.status == 404:
            raise _json_not_found("opencode_session_not_found", detail=_opencode_detail(exc))
        raise _json_bad_gateway("opencode_edit_failed", detail=_opencode_detail(exc))
    except Exception as exc:
        raise _json_bad_gateway("opencode_edit_failed", detail=_unexpected_upstream_detail(exc))
    idx = _find_message_index(old_messages, mid)
    if idx < 0:
        raise _json_not_found("message_not_found")
    if _message_role(old_messages[idx]) != "user":
        raise web.HTTPBadRequest(text=json.dumps({"error": "only_user_message_edit_supported"}), content_type="application/json")
    updated_record, _, metadata = await _delete_from_here(store=store, client=client, portal_session_id=sid, message_id=mid, allow_revert_fallback=bool(body.get("allow_revert_fallback", False)))
    try:
        before_messages = await client.list_messages(updated_record.opencode_session_id)
    except OpenCodeClientError as exc:
        raise _json_bad_gateway("opencode_edit_failed", detail=_opencode_detail(exc), metadata=metadata)
    except Exception as exc:
        raise _json_bad_gateway("opencode_edit_failed", detail=_unexpected_upstream_detail(exc), metadata=metadata)
    try:
        response_payload = await client.send_message(updated_record.opencode_session_id, parts=[{"type": "text", "text": content}], model=body.get("model") or updated_record.model, agent=body.get("agent") or updated_record.agent, system=body.get("system"))
    except OpenCodeClientError as exc:
        raise _json_bad_gateway("opencode_edit_resend_failed", detail=_opencode_detail(exc), metadata=metadata)
    except Exception as exc:
        raise _json_bad_gateway("opencode_edit_resend_failed", detail=_unexpected_upstream_detail(exc), metadata=metadata)
    try:
        after_messages = await client.list_messages(updated_record.opencode_session_id)
    except OpenCodeClientError as exc:
        raise _json_bad_gateway("opencode_edit_failed", detail=_opencode_detail(exc), metadata=metadata)
    except Exception as exc:
        raise _json_bad_gateway("opencode_edit_failed", detail=_unexpected_upstream_detail(exc), metadata=metadata)
    before_ids = {_message_id(message) for message in before_messages}
    replacement_user_message_id = ""
    assistant_message_id = ""
    for message in after_messages:
        message_id = _message_id(message)
        if not message_id or message_id in before_ids:
            continue
        role = _message_role(message)
        if role == "user":
            replacement_user_message_id = message_id
        elif role == "assistant":
            assistant_message_id = message_id
    display_store = request.app.get(USER_DISPLAY_STORE_KEY)
    if display_store is not None and replacement_user_message_id:
        try:
            display_store.put_user_message(
                portal_session_id=sid,
                opencode_session_id=updated_record.opencode_session_id,
                opencode_message_id=replacement_user_message_id,
                display_content=content,
                display_attachments=[],
                metadata={
                    "source": "portal_original_user_message",
                    "edited_from_message_id": mid,
                    "internal_model_content_hidden": True,
                },
            )
        except Exception:
            logger.warning("failed to save edited user display message", exc_info=True)
    assistant_text = _last_message_text(after_messages) or message_to_text(response_payload)
    store.update_after_chat(sid, content, assistant_text, body.get("model") or updated_record.model, body.get("agent") or updated_record.agent)
    return web.json_response({
        "success": True,
        "session_id": sid,
        "message_id": mid,
        "replacement_user_message_id": replacement_user_message_id,
        "assistant_message_id": assistant_message_id,
        "response": assistant_text,
        "messages": _to_efp_messages(
            after_messages,
            display_store=request.app.get(USER_DISPLAY_STORE_KEY),
            portal_session_id=sid,
            opencode_session_id=updated_record.opencode_session_id,
        ),
        "metadata": metadata,
    })
