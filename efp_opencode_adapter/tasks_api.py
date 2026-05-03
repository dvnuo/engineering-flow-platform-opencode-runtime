from __future__ import annotations

import asyncio
import os
import time
from typing import Any
from uuid import uuid4

from aiohttp import web

from .chat_api import extract_assistant_text
from .opencode_client import OpenCodeClientError
from .session_store import SessionRecord
from .task_completion_parser import parse_task_completion
from .task_prompts import build_task_prompt
from .task_store import TaskRecord, TaskStore, is_valid_task_id, utc_now_iso

TERMINAL = {"success", "error", "blocked", "cancelled"}


def _extract_session_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("id", "session_id", "uuid"):
        val = payload.get(key)
        if isinstance(val, str) and val:
            return val
    for key in ("session", "data", "info"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            sid = _extract_session_id(nested)
            if sid:
                return sid
    return ""


def _extract_response_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("message_id", "id", "uuid"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("message", "data", "info"):
        nested = payload.get(key)
        rid = _extract_response_id(nested)
        if rid:
            return rid
    return None


def _message_role(payload: Any) -> str | None:
    if isinstance(payload, dict):
        if isinstance(payload.get("role"), str):
            return payload["role"]
        info = payload.get("info")
        if isinstance(info, dict) and isinstance(info.get("role"), str):
            return info["role"]
        message = payload.get("message")
        if isinstance(message, dict):
            return _message_role(message)
    return None


def _assistant_text_from_messages(messages: list[dict[str, Any]], start: int) -> str | None:
    window = messages[start:] if start >= 0 else messages
    assistant_messages = [m for m in window if _message_role(m) == "assistant"]
    text = extract_assistant_text(assistant_messages)
    return text or None


def _assistant_text_from_event(event: dict[str, Any]) -> str | None:
    candidates: list[dict[str, Any]] = []
    if _message_role(event) == "assistant":
        candidates.append(event)
    for key in ("message", "data"):
        val = event.get(key)
        if isinstance(val, dict):
            if _message_role(val) == "assistant":
                candidates.append(val)
            nested_message = val.get("message")
            if isinstance(nested_message, dict) and _message_role(nested_message) == "assistant":
                candidates.append(nested_message)
    return extract_assistant_text(candidates) or None


def _event_matches_task(record: TaskRecord, event: dict[str, Any]) -> bool:
    if event.get("task_id") == record.task_id:
        return True
    session_candidates = [event.get("session_id"), event.get("opencode_session_id")]
    for key in ("message", "data"):
        val = event.get(key)
        if isinstance(val, dict):
            session_candidates.extend([val.get("session_id"), val.get("opencode_session_id")])
    for candidate in session_candidates:
        if isinstance(candidate, str) and candidate and candidate == record.opencode_session_id:
            return True
    return False


def _permission_id_from_event(event: dict[str, Any]) -> str | None:
    event_type = str(event.get("type") or event.get("event") or "").lower()
    if "permission" not in event_type:
        return None
    if any(done in event_type for done in ("granted", "approved", "denied", "resolved", "closed")):
        return None
    if not any(req in event_type for req in ("request", "pending", "created", "open")):
        return None
    for key in ("permission_id", "permissionID", "id"):
        val = event.get(key)
        if isinstance(val, str) and val:
            return val
    permission = event.get("permission")
    if isinstance(permission, dict):
        for key in ("id", "permission_id", "permissionID"):
            val = permission.get(key)
            if isinstance(val, str) and val:
                return val
    return "permission-request"


def _public_status_and_payload(record: TaskRecord) -> tuple[str, dict[str, Any] | None]:
    if record.status == "cancelled":
        out = dict(record.output_payload or {})
        out["error_code"] = out.get("error_code") or "cancelled"
        return "error", out
    return record.status, record.output_payload


def _to_public(record: TaskRecord) -> dict[str, Any]:
    status, output_payload = _public_status_and_payload(record)
    return {
        "ok": status not in {"error", "blocked"},
        "task_id": record.task_id,
        "execution_type": "task",
        "request_id": record.request_id,
        "status": status,
        "output_payload": output_payload or {},
        "artifacts": record.artifacts,
        "runtime_events": record.runtime_events,
        "next_action_hint": None,
        "audit_ref": None,
        "error": record.error,
        "engine": "opencode",
        "metadata": {"portal_session_id": record.portal_session_id, "opencode_session_id": record.opencode_session_id, "task_type": record.task_type},
    }


async def _publish_task_event(app: web.Application, record: TaskRecord, event_type: str, state: str) -> None:
    event = {"type": event_type, "engine": "opencode", "task_id": record.task_id, "request_id": record.request_id, "state": state, "status": state, "session_id": record.portal_session_id, "opencode_session_id": record.opencode_session_id, "timestamp": utc_now_iso()}
    md = record.metadata or {}
    gid = md.get("group_id") or md.get("portal_group_id")
    cid = md.get("coordination_run_id") or md.get("portal_coordination_run_id")
    if gid:
        event["group_id"] = gid
    if cid:
        event["coordination_run_id"] = cid
    store: TaskStore = app["task_store"]
    store.append_event(record.task_id, event)
    await app["event_bus"].publish(event)


async def _ensure_session(request: web.Request, portal_session_id: str, task_type: str, task_id: str) -> SessionRecord:
    store = request.app["session_store"]
    client = request.app["opencode_client"]
    record = store.get(portal_session_id)
    if record is None:
        created = await client.create_session(title=f"Task {task_type}: {task_id}")
        sid = _extract_session_id(created)
        if not sid:
            raise OpenCodeClientError("create_session returned no session id", status=502, payload=created)
        now = utc_now_iso()
        record = SessionRecord(portal_session_id, sid, f"Task {task_type}: {task_id}", None, None, now, now, "", 0)
        store.upsert(record)
        return record
    try:
        await client.get_session(record.opencode_session_id)
    except OpenCodeClientError as exc:
        if exc.status != 404:
            raise
        created = await client.create_session(title=record.title)
        sid = _extract_session_id(created)
        if not sid:
            raise OpenCodeClientError("create_session returned no session id", status=502, payload=created)
        record = SessionRecord(**{**record.__dict__, "opencode_session_id": sid, "partial_recovery": True, "updated_at": utc_now_iso()})
        store.upsert(record)
    return record


async def execute_task_handler(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "payload_must_be_object"}, status=400)
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        return web.json_response({"error": "task_id_required"}, status=400)
    if not is_valid_task_id(task_id):
        return web.json_response({"error": "invalid_task_id"}, status=400)
    task_type = payload.get("task_type")
    if not isinstance(task_type, str) or not task_type.strip():
        return web.json_response({"error": "task_type_required"}, status=400)

    input_payload = payload.get("input_payload") or {}
    metadata = payload.get("metadata") or {}
    if not isinstance(input_payload, dict):
        return web.json_response({"error": "input_payload_must_be_object"}, status=400)
    if not isinstance(metadata, dict):
        return web.json_response({"error": "metadata_must_be_object"}, status=400)

    source = payload.get("source")
    shared_context_ref = payload.get("shared_context_ref")
    context_ref = payload.get("context_ref")
    portal_session_id = payload.get("session_id") or f"task-{task_id}"
    request_id = payload.get("request_id") or f"task-{uuid4()}"

    task_store: TaskStore = request.app["task_store"]
    existing = task_store.get(task_id)
    if existing:
        if existing.status in {"accepted", "running"}:
            return web.json_response({"ok": True, "status": "accepted", "task_id": task_id, "request_id": existing.request_id}, status=202)
        return web.json_response(_to_public(existing), status=200)

    client = request.app["opencode_client"]
    try:
        session_record = await _ensure_session(request, portal_session_id, task_type, task_id)
        prompt = build_task_prompt(task_id=task_id, task_type=task_type, input_payload=input_payload, metadata=metadata, source=source, shared_context_ref=shared_context_ref, context_ref=context_ref)
        record = TaskRecord(task_id=task_id, task_type=task_type, request_id=request_id, status="accepted", portal_session_id=portal_session_id, opencode_session_id=session_record.opencode_session_id, input_payload=input_payload, metadata=metadata, output_payload={}, artifacts={}, runtime_events=[], error=None, created_at=utc_now_iso(), source=source, shared_context_ref=shared_context_ref, context_ref=context_ref)
        task_store.save(record)
        await _publish_task_event(request.app, record, "task.accepted", "accepted")

        msgs = await client.list_messages(record.opencode_session_id)
        record = task_store.update(task_id, message_cursor=len(msgs) if isinstance(msgs, list) else None)

        runtime_profile = metadata.get("runtime_profile") if isinstance(metadata.get("runtime_profile"), dict) else {}
        prompt_payload: dict[str, Any] = {"parts": [{"type": "text", "text": prompt}]}
        if runtime_profile.get("model"):
            prompt_payload["model"] = runtime_profile.get("model")
        if runtime_profile.get("agent"):
            prompt_payload["agent"] = runtime_profile.get("agent")
        if metadata.get("system_prompt"):
            prompt_payload["system"] = metadata.get("system_prompt")

        prompt_result = await client.prompt_async(record.opencode_session_id, prompt_payload)
        prompt_id = _extract_response_id(prompt_result)
        record = task_store.update(task_id, status="running", started_at=utc_now_iso(), opencode_prompt_id=prompt_id, opencode_message_id=prompt_id)
        await _publish_task_event(request.app, record, "task.started", "running")
        schedule_task_collector(request.app, task_id)
        return web.json_response({"ok": True, "status": "accepted", "task_id": task_id, "request_id": request_id}, status=202)
    except OpenCodeClientError as exc:
        record = TaskRecord(task_id=task_id, task_type=task_type, request_id=request_id, status="error", portal_session_id=portal_session_id, opencode_session_id="", input_payload=input_payload, metadata=metadata, output_payload={"summary": "OpenCode request failed", "error_code": "opencode_error"}, artifacts={}, runtime_events=[], error={"message": str(exc)}, created_at=utc_now_iso(), finished_at=utc_now_iso(), source=source, shared_context_ref=shared_context_ref, context_ref=context_ref)
        task_store.save(record)
        await _publish_task_event(request.app, record, "task.completed", "error")
        return web.json_response({"error": "opencode_error"}, status=502)


def schedule_task_collector(app: web.Application, task_id: str) -> None:
    bg = asyncio.create_task(collect_task_completion(app, task_id))
    app["task_background_tasks"].add(bg)
    bg.add_done_callback(app["task_background_tasks"].discard)


async def _try_read_completion_from_messages(record: TaskRecord, client: Any) -> str | None:
    messages = await client.list_messages(record.opencode_session_id)
    if not isinstance(messages, list):
        return None
    start = record.message_cursor or 0
    return _assistant_text_from_messages(messages, start)


async def _try_read_completion_from_events(app: web.Application, record: TaskRecord, max_seconds: float) -> tuple[str | None, list[dict[str, Any]]]:
    client = app["opencode_client"]
    observed: list[dict[str, Any]] = []
    if not hasattr(client, "event_stream"):
        return None, observed
    timeout = max(1, int(max_seconds))
    try:
        async for event in client.event_stream(global_events=True, timeout_seconds=timeout):
            if not isinstance(event, dict):
                continue
            if not _event_matches_task(record, event):
                continue
            observed.append(event)
            text = _assistant_text_from_event(event)
            if text:
                return text, observed
    except Exception:
        return None, observed
    return None, observed


async def collect_task_completion(app: web.Application, task_id: str) -> None:
    timeout = float(os.getenv("EFP_TASK_COMPLETION_TIMEOUT_SECONDS", "900"))
    poll = float(os.getenv("EFP_TASK_COMPLETION_POLL_SECONDS", "1.0"))
    deadline = time.monotonic() + timeout
    store: TaskStore = app["task_store"]
    client = app["opencode_client"]
    try:
        while time.monotonic() < deadline:
            record = store.get(task_id)
            if record is None or record.status in TERMINAL:
                return

            remaining = max(0.05, min(1.0, deadline - time.monotonic()))
            event_text, observed = await _try_read_completion_from_events(app, record, remaining)
            pending = list(record.pending_permission_ids or [])
            for evt in observed:
                pid = _permission_id_from_event(evt)
                if pid and pid not in pending:
                    pending.append(pid)
            if pending != (record.pending_permission_ids or []):
                record = store.update(task_id, pending_permission_ids=pending)

            if event_text:
                status, output_payload, error = parse_task_completion(event_text, task_type=record.task_type, input_payload=record.input_payload, metadata=record.metadata)
                record = store.update(task_id, status=status, output_payload=output_payload, error=error, finished_at=utc_now_iso(), completion_source="opencode_event")
                await _publish_task_event(app, record, "task.completed", status)
                return

            message_text = await _try_read_completion_from_messages(record, client)
            if message_text:
                status, output_payload, error = parse_task_completion(message_text, task_type=record.task_type, input_payload=record.input_payload, metadata=record.metadata)
                record = store.update(task_id, status=status, output_payload=output_payload, error=error, finished_at=utc_now_iso(), completion_source="messages")
                await _publish_task_event(app, record, "task.completed", status)
                return
            await asyncio.sleep(poll)

        record = store.get(task_id)
        if record is None or record.status in TERMINAL:
            return
        if record.pending_permission_ids:
            out = {
                "summary": "Task blocked waiting for unresolved OpenCode permission request",
                "error_code": "permission_request_timeout",
                "pending_permission_ids": record.pending_permission_ids,
                "artifacts": [],
                "blockers": ["OpenCode permission request was not resolved before task timeout"],
                "next_recommendation": "Resolve or pre-authorize the required tool permission, then re-dispatch the task.",
                "audit_trace": [],
                "external_actions": [],
            }
        else:
            out = dict(record.output_payload or {})
            out["error_code"] = "task_completion_timeout"
            out.setdefault("summary", "Task timed out waiting for completion")
        record = store.update(task_id, status="blocked", output_payload=out, finished_at=utc_now_iso())
        await _publish_task_event(app, record, "task.completed", "blocked")
    except Exception as exc:
        record = store.get(task_id)
        if record is None:
            return
        out = dict(record.output_payload or {})
        out.setdefault("summary", "Task execution failed")
        record = store.update(task_id, status="error", output_payload=out, error={"message": str(exc)}, finished_at=utc_now_iso())
        await _publish_task_event(app, record, "task.completed", "error")


async def cleanup_task_background_tasks(app: web.Application) -> None:
    tasks = list(app.get("task_background_tasks", set()))
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def get_task_handler(request: web.Request) -> web.Response:
    task_id = request.match_info["task_id"]
    if not is_valid_task_id(task_id):
        return web.json_response({"error": "invalid_task_id"}, status=400)
    store: TaskStore = request.app["task_store"]
    record = store.get(task_id)
    if record is None:
        return web.json_response({"error": "task_not_found"}, status=404)
    return web.json_response(_to_public(record))
