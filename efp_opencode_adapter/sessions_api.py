from __future__ import annotations

import json
from typing import Any

from aiohttp import web
from .app_keys import CHATLOG_STORE_KEY, OPENCODE_CLIENT_KEY, PORTAL_METADATA_CLIENT_KEY, SESSION_STORE_KEY

from .opencode_client import OpenCodeClientError
from .opencode_message_adapter import message_to_visible_text, to_efp_message
from .thinking_events import safe_preview


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


def _is_internal_efp_message(message: Any) -> bool:
    mid = _message_id(message)
    if mid.startswith("efp-auto-continue-"):
        return True
    for part in _message_parts(message):
        metadata = part.get("metadata")
        if isinstance(metadata, dict) and metadata.get("efp_internal") == "auto_continue":
            return True
    return False


def _to_efp_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    filtered = [msg for msg in messages if not _is_internal_efp_message(msg)]
    return [to_efp_message(msg, index=idx) for idx, msg in enumerate(filtered)]


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
    efp_messages = _to_efp_messages(messages)
    return web.json_response(
        {
            "session_id": sid,
            "name": record.title,
            "messages": efp_messages,
            "metadata": {
                "engine": "opencode",
                "opencode_session_id": record.opencode_session_id,
                "partial_recovery": record.partial_recovery,
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
    record = store.get(sid)
    if record is None or record.deleted:
        metadata_delete = await _delete_portal_metadata_best_effort(portal_metadata, sid)
        return web.json_response({"success": True, "session_id": sid, "already_deleted": True, "runtime_deleted": False, "opencode_deleted": False, "opencode_missing": record is None, "metadata_delete": metadata_delete})
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
    chatlog_delete = _delete_chatlog_best_effort(chatlog_store, sid)
    metadata_delete = await _delete_portal_metadata_best_effort(portal_metadata, sid)
    return web.json_response({"success": True, "session_id": sid, "opencode_session_id": record.opencode_session_id, "already_deleted": False, "runtime_deleted": True, "opencode_deleted": opencode_deleted, "opencode_missing": opencode_missing, "metadata_delete": metadata_delete, "chatlog_delete": chatlog_delete})


async def clear_sessions_handler(request: web.Request) -> web.Response:
    store = request.app[SESSION_STORE_KEY]
    client = request.app[OPENCODE_CLIENT_KEY]
    portal_metadata = request.app.get(PORTAL_METADATA_CLIENT_KEY)
    chatlog_store = request.app.get(CHATLOG_STORE_KEY)
    failures = []
    deleted_count = 0
    metadata_results = []
    for rec in store.list_active():
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
        chatlog_delete = _delete_chatlog_best_effort(chatlog_store, rec.portal_session_id)
        metadata = await _delete_portal_metadata_best_effort(portal_metadata, rec.portal_session_id)
        metadata_results.append({"session_id": rec.portal_session_id, "opencode_missing": op_missing, "metadata_delete": metadata, "chatlog_delete": chatlog_delete})
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
    _, new_messages, metadata = await _delete_from_here(store=request.app[SESSION_STORE_KEY], client=request.app[OPENCODE_CLIENT_KEY], portal_session_id=sid, message_id=mid, allow_revert_fallback=allow_revert_fallback)
    return web.json_response({"success": True, "session_id": sid, "message_id": mid, "engine": "opencode", "mutation": "delete_from_here", "messages": _to_efp_messages(new_messages), "metadata": metadata})


async def edit_message_handler(request: web.Request) -> web.Response:
    sid = request.match_info["session_id"]
    mid = request.match_info["message_id"]
    body = await _read_json_object(request)
    content = ""
    for key in ("content", "new_content", "message"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            content = value.strip()
            break
    if not content:
        raise web.HTTPBadRequest(text=json.dumps({"error": "content_required"}), content_type="application/json")
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
    assistant_text = _last_message_text(after_messages) or message_to_text(response_payload)
    store.update_after_chat(sid, content, assistant_text, body.get("model") or updated_record.model, body.get("agent") or updated_record.agent)
    return web.json_response({"success": True, "session_id": sid, "message_id": mid, "replacement_user_message_id": replacement_user_message_id, "assistant_message_id": assistant_message_id, "response": assistant_text, "messages": _to_efp_messages(after_messages), "metadata": metadata})
