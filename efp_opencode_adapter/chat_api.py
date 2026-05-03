from __future__ import annotations

import json
import re
from typing import Any
from uuid import uuid4

from aiohttp import web

from .opencode_client import OpenCodeClientError
from .session_store import SessionRecord
from .thinking_events import (
    assistant_delta_event,
    chat_complete_event,
    chat_completed_compat_event,
    chat_failed_event,
    chat_started_event,
    llm_thinking_event,
    safe_preview,
    utc_now_iso,
)


def _bad_request(error: str) -> web.HTTPBadRequest:
    return web.HTTPBadRequest(text=json.dumps({"error": error}), content_type="application/json")


def _metadata_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata")
    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise _bad_request("metadata_must_be_object")
    return metadata


def _runtime_profile_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    runtime_profile = metadata.get("runtime_profile")
    if runtime_profile is None:
        return {}
    if not isinstance(runtime_profile, dict):
        raise _bad_request("runtime_profile_must_be_object")
    return runtime_profile


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _normalize_title(raw: Any) -> str:
    return re.sub(r"\s+", " ", raw if isinstance(raw, str) else "").strip() or "Chat"


def _extract_session_id(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("id", "session_id", "uuid"):
            if payload.get(key):
                return str(payload[key])
    return ""


def _require_opencode_session_id(payload: Any, *, action: str) -> str:
    sid = _extract_session_id(payload)
    if not sid:
        raise OpenCodeClientError(f"{action} returned no session id", payload=safe_preview(payload, 1000))
    return sid


def _optional_nonempty_string_from_payload(payload: dict[str, Any], key: str, *, generated: str, error: str) -> str:
    if key not in payload or payload.get(key) is None:
        return generated
    value = payload.get(key)
    if not isinstance(value, str):
        raise _bad_request(error)
    return value.strip() or generated


def _portal_session_id_from_payload(payload: dict[str, Any]) -> str:
    return _optional_nonempty_string_from_payload(payload, "session_id", generated=str(uuid4()), error="session_id_must_be_string")


def _request_id_from_payload(payload: dict[str, Any]) -> str:
    return _optional_nonempty_string_from_payload(payload, "request_id", generated=f"chat-{uuid4()}", error="request_id_must_be_string")


def _parts_to_text(parts: Any) -> str:
    if not isinstance(parts, list):
        return ""
    out: list[str] = []
    for part in parts:
        if isinstance(part, dict):
            if isinstance(part.get("text"), str):
                out.append(part["text"])
            elif isinstance(part.get("content"), str):
                out.append(part["content"])
        elif isinstance(part, str):
            out.append(part)
    return "\n".join(x for x in out if x).strip()


def _message_role(message: dict[str, Any]) -> str:
    info = message.get("info")
    if isinstance(info, dict) and isinstance(info.get("role"), str):
        return info["role"]
    if isinstance(message.get("role"), str):
        return message["role"]
    nested = message.get("message")
    if isinstance(nested, dict):
        return _message_role(nested)
    return ""


def _message_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if not isinstance(message, dict):
        return ""
    text = _parts_to_text(message.get("parts"))
    if text:
        return text
    nested = message.get("message")
    if isinstance(nested, dict):
        text = _message_text(nested)
        if text:
            return text
    for key in ("response", "text", "content"):
        if isinstance(message.get(key), str) and message[key]:
            return message[key]
    return ""


def extract_assistant_text(payload: Any) -> str:
    if isinstance(payload, list):
        for msg in reversed(payload):
            if isinstance(msg, dict) and _message_role(msg) == "assistant":
                text = _message_text(msg)
                if text:
                    return text
        for msg in reversed(payload):
            text = _message_text(msg)
            if text:
                return text
        return ""
    if isinstance(payload, dict):
        nested = payload.get("message")
        if isinstance(nested, (dict, list, str)):
            text = extract_assistant_text(nested)
            if text:
                return text
        text = _message_text(payload)
        if text:
            return text
        for key in ("messages", "data"):
            if isinstance(payload.get(key), list):
                text = extract_assistant_text(payload[key])
                if text:
                    return text
    return ""


async def _ensure_record_for_chat(*, client, store, portal_session_id: str, title: str, agent: str | None, model: str | None) -> tuple[SessionRecord, bool]:
    existing = store.get(portal_session_id)
    if existing is None:
        created = await client.create_session(title=title)
        sid = _require_opencode_session_id(created, action="create_session")
        now = utc_now_iso()
        record = SessionRecord(portal_session_id, sid, title, agent, model, now, now, "", 0, False, False)
        store.upsert(record)
        return record, False

    try:
        await client.get_session(existing.opencode_session_id)
        return existing, bool(existing.partial_recovery)
    except OpenCodeClientError as exc:
        if exc.status != 404:
            raise

    created = await client.create_session(title=existing.title or title)
    sid = _require_opencode_session_id(created, action="partial_recovery_create_session")
    recovered = SessionRecord(
        existing.portal_session_id,
        sid,
        existing.title or title,
        agent or existing.agent,
        model or existing.model,
        existing.created_at,
        utc_now_iso(),
        existing.last_message,
        existing.message_count,
        False,
        True,
    )
    store.upsert(recovered)
    return recovered, True


async def handle_chat_payload(request: web.Request, payload: dict[str, Any]) -> dict[str, Any]:
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        raise _bad_request("message_required")

    metadata = _metadata_from_payload(payload)
    runtime_profile = _runtime_profile_from_metadata(metadata)
    portal_session_id = _portal_session_id_from_payload(payload)
    request_id = _request_id_from_payload(payload)
    title = _normalize_title(_optional_str(metadata.get("title")) or message[:60])
    model = _optional_str(metadata.get("model")) or _optional_str(runtime_profile.get("model"))
    agent = _optional_str(metadata.get("agent"))
    system = _optional_str(metadata.get("system"))

    store = request.app["session_store"]
    bus = request.app["event_bus"]
    client = request.app["opencode_client"]
    chatlog_store = request.app["chatlog_store"]
    usage_tracker = request.app["usage_tracker"]
    portal_metadata_client = request.app["portal_metadata_client"]

    runtime_events: list[dict[str, Any]] = []
    context_state = {"objective": message[:300], "summary": "OpenCode request accepted", "current_state": "running", "next_step": "Waiting for OpenCode assistant response", "constraints": [], "decisions": [], "open_loops": [], "budget": {"usage_percent": 0}}

    existing_record = store.get(portal_session_id)
    opencode_session_id = existing_record.opencode_session_id if existing_record else ""
    try:
        record, partial_recovery = await _ensure_record_for_chat(client=client, store=store, portal_session_id=portal_session_id, title=title, agent=agent, model=model)
        opencode_session_id = record.opencode_session_id

        start = chat_started_event(session_id=portal_session_id, request_id=request_id, opencode_session_id=record.opencode_session_id)
        runtime_events.append(start)
        await bus.publish(start)

        chatlog_store.start_entry(portal_session_id, request_id=request_id, message=message, runtime_events=runtime_events, context_state=context_state, llm_debug={"engine": "opencode", "opencode_session_id": record.opencode_session_id})

        await portal_metadata_client.publish_session_metadata(session_id=portal_session_id, latest_event_type="chat.started", latest_event_state="running", request_id=request_id, summary="Chat started", runtime_events=runtime_events, metadata={"opencode_session_id": record.opencode_session_id})

        think = llm_thinking_event(session_id=portal_session_id, request_id=request_id, opencode_session_id=record.opencode_session_id)
        runtime_events.append(think)
        await bus.publish(think)

        response_payload = await client.send_message(record.opencode_session_id, parts=[{"type": "text", "text": message}], model=model, agent=agent, system=system)
        assistant_text = extract_assistant_text(response_payload) or "[no assistant response]"

        for event in [
            assistant_delta_event(session_id=portal_session_id, request_id=request_id, opencode_session_id=record.opencode_session_id, text=assistant_text),
            chat_complete_event(session_id=portal_session_id, request_id=request_id, opencode_session_id=record.opencode_session_id, text=assistant_text),
            chat_completed_compat_event(session_id=portal_session_id, request_id=request_id, opencode_session_id=record.opencode_session_id, text=assistant_text),
        ]:
            runtime_events.append(event)
            await bus.publish(event)

        updated = store.update_after_chat(portal_session_id, message, assistant_text, model, agent)
        provider = _optional_str(runtime_profile.get("provider")) or _optional_str(metadata.get("provider")) or (response_payload.get("provider") if isinstance(response_payload, dict) else None) or "unknown"
        usage_record = usage_tracker.record_chat(session_id=portal_session_id, request_id=request_id, model=model, provider=provider, response_payload=response_payload, input_text=message, output_text=assistant_text)
        final_context = {**context_state, "summary": assistant_text[:500], "current_state": "completed", "next_step": ""}

        chatlog_store.finish_entry(portal_session_id, request_id=request_id, status="success", response=assistant_text, runtime_events=runtime_events, events=runtime_events, context_state=final_context, llm_debug={"engine": "opencode", "opencode_session_id": updated.opencode_session_id, "usage": usage_record, "response_payload_preview": safe_preview(response_payload, 2000)})

        await portal_metadata_client.publish_session_metadata(session_id=portal_session_id, latest_event_type="chat.completed", latest_event_state="success", request_id=request_id, summary=assistant_text[:300], runtime_events=runtime_events, metadata={"engine": "opencode", "opencode_session_id": updated.opencode_session_id, "model": model, "provider": provider, "context_state": final_context, "usage": usage_record})

    except OpenCodeClientError as exc:
        failed = chat_failed_event(session_id=portal_session_id, request_id=request_id, opencode_session_id=opencode_session_id, error=str(exc))
        runtime_events.append(failed)
        await bus.publish(failed)
        chatlog_store.fail_entry(portal_session_id, request_id=request_id, error=str(exc), runtime_events=runtime_events, context_state=context_state, llm_debug={"engine": "opencode"})
        await portal_metadata_client.publish_session_metadata(session_id=portal_session_id, latest_event_type="chat.failed", latest_event_state="error", request_id=request_id, summary=str(exc), runtime_events=runtime_events, metadata={"engine": "opencode"})
        raise web.HTTPBadGateway(text=json.dumps({"error": "opencode_error", "detail": str(exc)}), content_type="application/json")

    out = {"session_id": portal_session_id, "request_id": request_id, "response": assistant_text, "events": runtime_events, "runtime_events": runtime_events, "usage": usage_record, "context_state": final_context, "_llm_debug": {"engine": "opencode", "opencode_session_id": updated.opencode_session_id, "usage": usage_record, "thinking_events": runtime_events}}
    if partial_recovery or getattr(updated, "partial_recovery", False):
        out["_llm_debug"]["partial_recovery"] = True
    return out


async def chat_handler(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:
        raise _bad_request("invalid_json")
    if not isinstance(payload, dict):
        raise _bad_request("invalid_json")
    return web.json_response(await handle_chat_payload(request, payload))


async def _stream_error_response(request: web.Request, error: str, detail: str | None = None) -> web.StreamResponse:
    resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache", "Connection": "keep-alive"})
    await resp.prepare(request)
    await resp.write(f"event: error\ndata: {json.dumps({'error': error, 'detail': detail or error}, ensure_ascii=False)}\n\n".encode())
    await resp.write_eof()
    return resp


async def chat_stream_handler(request: web.Request) -> web.StreamResponse:
    try:
        payload = await request.json()
    except Exception:
        return await _stream_error_response(request, "invalid_json")
    if not isinstance(payload, dict):
        return await _stream_error_response(request, "invalid_json")

    try:
        session_id = _portal_session_id_from_payload(payload)
        req_id = _request_id_from_payload(payload)
    except web.HTTPException as exc:
        err = "chat_failed"
        try:
            j = json.loads(exc.text or "{}")
            if isinstance(j, dict) and isinstance(j.get("error"), str):
                err = j["error"]
        except Exception:
            pass
        return await _stream_error_response(request, err, exc.text)

    payload = {**payload, "session_id": session_id, "request_id": req_id}
    resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache", "Connection": "keep-alive"})
    await resp.prepare(request)

    pre = chat_started_event(session_id=session_id, request_id=req_id)
    await resp.write(f"event: runtime_event\ndata: {json.dumps(pre, ensure_ascii=False)}\n\n".encode())
    try:
        result = await handle_chat_payload(request, payload)
        for event in result.get("runtime_events", []):
            if event.get("type") == pre.get("type") and event.get("session_id") == pre.get("session_id") and event.get("request_id") == pre.get("request_id"):
                continue
            await resp.write(f"event: runtime_event\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
        await resp.write(f"event: final\ndata: {json.dumps(result, ensure_ascii=False)}\n\n".encode())
        await resp.write(b'event: done\ndata: {"ok":true}\n\n')
    except web.HTTPException as exc:
        await resp.write(f"event: error\ndata: {json.dumps({'error': 'chat_failed', 'detail': exc.text}, ensure_ascii=False)}\n\n".encode())
    await resp.write_eof()
    return resp
