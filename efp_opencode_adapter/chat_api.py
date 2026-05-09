from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Any
from uuid import uuid4

from aiohttp import web
from .app_keys import (
    ATTACHMENT_SERVICE_KEY,
    CHATLOG_STORE_KEY,
    EVENT_BUS_KEY,
    OPENCODE_CLIENT_KEY,
    PORTAL_METADATA_CLIENT_KEY,
    SETTINGS_KEY,
    SESSION_STORE_KEY,
    USAGE_TRACKER_KEY,
)

from .opencode_client import OpenCodeClientError
from .attachment_service import build_opencode_attachment_parts
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
from .trace_context import add_trace_context, build_trace_context, profile_version_from_metadata
from .opencode_config import normalize_opencode_provider_id
from .opencode_message_adapter import (
    extract_last_assistant_visible_text,
    extract_reasoning_texts_from_parts,
    message_id as adapter_message_id,
    message_role as adapter_message_role,
)

DATA_URL_RE = re.compile(r"data:[A-Za-z0-9.+/_-]+(?:;[A-Za-z0-9.+/_=-]+)*;base64,[A-Za-z0-9+/=]+")


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


def _model_from_chat_payload(payload: dict[str, Any], metadata: dict[str, Any], runtime_profile: dict[str, Any]) -> str | None:
    rp_cfg = ((metadata.get("runtime_profile") or {}).get("config") if isinstance(metadata.get("runtime_profile"), dict) else {}) or {}
    llm_cfg = rp_cfg.get("llm") if isinstance(rp_cfg, dict) and isinstance(rp_cfg.get("llm"), dict) else {}
    model_candidates = [payload.get("model"), payload.get("model_override"), metadata.get("model"), runtime_profile.get("model"), llm_cfg.get("model")]
    provider_candidates = [metadata.get("provider"), runtime_profile.get("provider"), llm_cfg.get("provider")]
    model = next((m for m in model_candidates if isinstance(m, str) and m.strip()), None)
    provider = next((p for p in provider_candidates if isinstance(p, str) and p.strip()), None)
    if not model:
        return None
    if "/" in model:
        prefix, suffix = model.split("/", 1)
        return f"{normalize_opencode_provider_id(prefix)}/{suffix}"
    if provider:
        return f"{normalize_opencode_provider_id(provider)}/{model}"
    return model


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


def _detect_new_message_ids(before_messages: list[dict[str, Any]], after_messages: list[dict[str, Any]]) -> tuple[str, str]:
    before_ids = {adapter_message_id(message) for message in before_messages if adapter_message_id(message)}
    user_message_id = ""
    assistant_message_id = ""
    for message in after_messages:
        message_id = adapter_message_id(message)
        if not message_id or message_id in before_ids:
            continue
        role = adapter_message_role(message).lower()
        if role == "user":
            user_message_id = message_id
        elif role == "assistant":
            assistant_message_id = message_id
    return user_message_id, assistant_message_id




def extract_assistant_text(payload: Any) -> str:
    return extract_last_assistant_visible_text(payload)


def _redact_attachment_payloads_for_debug(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "url" and isinstance(item, str) and item.startswith("data:"):
                out[key] = "data:<redacted>"
            else:
                out[key] = _redact_attachment_payloads_for_debug(item)
        return out
    if isinstance(value, list):
        return [_redact_attachment_payloads_for_debug(item) for item in value]
    if isinstance(value, str):
        return DATA_URL_RE.sub("data:<redacted>;base64,<redacted>", value)
    return value


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
    model = _model_from_chat_payload(payload, metadata, runtime_profile)
    agent = _optional_str(metadata.get("agent"))
    system = _optional_str(metadata.get("system"))
    attachments = payload.get("attachments")

    store = request.app[SESSION_STORE_KEY]
    bus = request.app[EVENT_BUS_KEY]
    client = request.app[OPENCODE_CLIENT_KEY]
    chatlog_store = request.app[CHATLOG_STORE_KEY]
    usage_tracker = request.app[USAGE_TRACKER_KEY]
    portal_metadata_client = request.app[PORTAL_METADATA_CLIENT_KEY]
    settings = request.app[SETTINGS_KEY]

    runtime_events: list[dict[str, Any]] = []
    context_state = {"objective": message[:300], "summary": "OpenCode request accepted", "current_state": "running", "next_step": "Waiting for OpenCode assistant response", "constraints": [], "decisions": [], "open_loops": [], "budget": {"usage_percent": 0}}

    existing_record = store.get(portal_session_id)
    opencode_session_id = existing_record.opencode_session_id if existing_record else ""
    provider_for_trace_raw = _optional_str(runtime_profile.get("provider")) or _optional_str(metadata.get("provider"))
    provider_for_trace = normalize_opencode_provider_id(provider_for_trace_raw)
    profile_version, runtime_profile_id = profile_version_from_metadata(metadata, runtime_profile)
    trace_context: dict[str, str] = {}
    try:
        record, partial_recovery = await _ensure_record_for_chat(client=client, store=store, portal_session_id=portal_session_id, title=title, agent=agent, model=model)
        opencode_session_id = record.opencode_session_id
        trace_context = build_trace_context(settings, request_id=request_id, session_id=portal_session_id, opencode_session_id=record.opencode_session_id, profile_version=profile_version, runtime_profile_id=runtime_profile_id, model=model or "", provider=provider_for_trace or "")

        start = add_trace_context(chat_started_event(session_id=portal_session_id, request_id=request_id, opencode_session_id=record.opencode_session_id), trace_context)
        runtime_events.append(start)
        await bus.publish(start)

        chatlog_store.start_entry(portal_session_id, request_id=request_id, message=message, runtime_events=runtime_events, context_state=context_state, llm_debug={"engine": "opencode", "opencode_session_id": record.opencode_session_id, "trace_context": trace_context})

        await portal_metadata_client.publish_session_metadata(session_id=portal_session_id, latest_event_type="chat.started", latest_event_state="running", request_id=request_id, summary="Chat started", runtime_events=runtime_events, metadata={"opencode_session_id": record.opencode_session_id, "trace_context": trace_context})

        think = add_trace_context(llm_thinking_event(session_id=portal_session_id, request_id=request_id, opencode_session_id=record.opencode_session_id), trace_context)
        runtime_events.append(think)
        await bus.publish(think)

        before_messages: list[dict[str, Any]] = []
        message_id_detection_error_before = ""
        try:
            before_messages = await client.list_messages(record.opencode_session_id)
        except Exception as exc:
            message_id_detection_error_before = str(exc)
        parts = [{"type": "text", "text": message}]
        attachment_debug = []
        attachment_service = request.app.get(ATTACHMENT_SERVICE_KEY)
        if attachments:
            try:
                attachment_parts, attachment_debug = build_opencode_attachment_parts(
                    attachment_service,
                    portal_session_id,
                    attachments,
                    max_text_chars=30000,
                    max_inline_bytes=10 * 1024 * 1024,
                )
                parts.extend(attachment_parts)
            except Exception as exc:
                safe_error = safe_preview(str(exc), 300)
                attachment_debug.append({"status": "error", "error": safe_error})
                parts.append({"type": "text", "text": f"Attachment processing failed: {safe_error}"})

        response_payload = await client.send_message(record.opencode_session_id, parts=parts, model=model, agent=agent, system=system)
        assistant_text = extract_last_assistant_visible_text(response_payload)
        payload_message = response_payload.get("message") if isinstance(response_payload, dict) else None
        if isinstance(payload_message, dict):
            reasoning_texts = extract_reasoning_texts_from_parts(payload_message.get("parts"))
            for item in reasoning_texts:
                think_event = add_trace_context({"type": "llm_thinking", "engine": "opencode", "session_id": portal_session_id, "request_id": request_id, "opencode_session_id": record.opencode_session_id, "message": safe_preview(item, 300), "data": {"message": safe_preview(item, 300)}, "created_at": utc_now_iso()}, trace_context)
                runtime_events.append(think_event)
                await bus.publish(think_event)
        user_message_id = ""
        assistant_message_id = ""
        try:
            after_messages = await client.list_messages(record.opencode_session_id)
            user_message_id, assistant_message_id = _detect_new_message_ids(before_messages, after_messages)
        except Exception:
            pass
        if not assistant_text:
            try:
                assistant_text = extract_last_assistant_visible_text(after_messages) if "after_messages" in locals() else ""
            except Exception:
                assistant_text = ""
        if not assistant_text:
            assistant_text = ""
        if not assistant_message_id and isinstance(response_payload, dict):
            candidate = response_payload.get("info", {}).get("id") if isinstance(response_payload.get("info"), dict) else ""
            if not candidate and isinstance(response_payload.get("message"), dict):
                candidate = adapter_message_id(response_payload["message"])
            assistant_message_id = str(candidate or "")

        out_events = [
            assistant_delta_event(session_id=portal_session_id, request_id=request_id, opencode_session_id=record.opencode_session_id, text=assistant_text),
            chat_complete_event(session_id=portal_session_id, request_id=request_id, opencode_session_id=record.opencode_session_id, text=assistant_text),
            chat_completed_compat_event(session_id=portal_session_id, request_id=request_id, opencode_session_id=record.opencode_session_id, text=assistant_text),
        ]
        if out_events and isinstance(out_events[0], dict):
            out_events[0]["synthetic_final_delta"] = True
            if not isinstance(out_events[0].get("data"), dict):
                out_events[0]["data"] = {}
            out_events[0]["data"]["synthetic_final_delta"] = True
        for event in [add_trace_context(x, trace_context) for x in out_events]:
            runtime_events.append(event)
            await bus.publish(event)

        updated = store.update_after_chat(portal_session_id, message, assistant_text, model, agent)
        provider = provider_for_trace
        usage_record = usage_tracker.record_chat(session_id=portal_session_id, request_id=request_id, model=model, provider=provider, response_payload=response_payload, input_text=message, output_text=assistant_text)
        usage_record["request_id"] = trace_context.get("request_id", usage_record.get("request_id", ""))
        final_context = {**context_state, "summary": assistant_text[:500], "current_state": "completed", "next_step": ""}

        llm_debug = {"engine": "opencode", "opencode_session_id": updated.opencode_session_id, "usage": usage_record, "response_payload_preview": safe_preview(_redact_attachment_payloads_for_debug(response_payload), 2000), "trace_context": trace_context, "message_ids": {"user_message_id": user_message_id or "", "assistant_message_id": assistant_message_id or ""}, "attachments": attachment_debug}
        if message_id_detection_error_before:
            llm_debug["message_id_detection_error_before"] = message_id_detection_error_before
        chatlog_store.finish_entry(portal_session_id, request_id=request_id, status="success", response=assistant_text, runtime_events=runtime_events, events=runtime_events, context_state=final_context, llm_debug=llm_debug)

        metadata_model = usage_record.get("model") or model or "unknown"
        metadata_provider = usage_record.get("provider") or provider or "unknown"
        await portal_metadata_client.publish_session_metadata(session_id=portal_session_id, latest_event_type="chat.completed", latest_event_state="success", request_id=request_id, summary=assistant_text[:300], runtime_events=runtime_events, metadata={"engine": "opencode", "opencode_session_id": updated.opencode_session_id, "model": metadata_model, "provider": metadata_provider, "context_state": final_context, "usage": usage_record, "trace_context": trace_context})

    except OpenCodeClientError as exc:
        if not trace_context:
            trace_context = build_trace_context(settings, request_id=request_id, session_id=portal_session_id, opencode_session_id=opencode_session_id, profile_version=profile_version, runtime_profile_id=runtime_profile_id, model=model or "", provider=provider_for_trace or "")
        failed = add_trace_context(chat_failed_event(session_id=portal_session_id, request_id=request_id, opencode_session_id=opencode_session_id, error=str(exc)), trace_context)
        runtime_events.append(failed)
        await bus.publish(failed)
        chatlog_store.fail_entry(portal_session_id, request_id=request_id, error=str(exc), runtime_events=runtime_events, context_state=context_state, llm_debug={"engine": "opencode", "opencode_session_id": opencode_session_id, "trace_context": trace_context})
        await portal_metadata_client.publish_session_metadata(session_id=portal_session_id, latest_event_type="chat.failed", latest_event_state="error", request_id=request_id, summary=str(exc), runtime_events=runtime_events, metadata={"engine": "opencode", "trace_context": trace_context})
        raise web.HTTPBadGateway(text=json.dumps({"error": "opencode_error", "detail": str(exc)}), content_type="application/json")

    out = {"session_id": portal_session_id, "request_id": trace_context.get("request_id", request_id), "response": assistant_text, "user_message_id": user_message_id or "", "assistant_message_id": assistant_message_id or "", "events": runtime_events, "runtime_events": runtime_events, "usage": usage_record, "context_state": final_context, "_llm_debug": {"engine": "opencode", "opencode_session_id": updated.opencode_session_id, "usage": usage_record, "thinking_events": runtime_events, "trace_context": trace_context, "attachments": attachment_debug}}
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


STREAM_HEARTBEAT_SECONDS = 15.0
BRIDGE_EVENT_TYPES = {"tool.started", "tool.completed", "tool.failed", "permission_request", "permission_resolved", "assistant_delta", "message.delta", "llm_thinking", "opencode.reasoning", "provider.retry", "provider.status", "execution.started", "execution.completed", "execution.failed", "complete", "final", "error"}


class SSEClientDisconnected(Exception):
    pass


def _is_closed_transport_runtime_error(exc: RuntimeError) -> bool:
    lowered = str(exc).lower()
    return "closing transport" in lowered or "closed" in lowered


def _sse_encode(event_name: str, payload: dict[str, Any]) -> bytes:
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


async def _write_sse(resp: web.StreamResponse, event_name: str, payload: dict[str, Any]) -> None:
    try:
        await resp.write(_sse_encode(event_name, payload))
    except (ConnectionResetError, BrokenPipeError) as exc:
        raise SSEClientDisconnected() from exc
    except RuntimeError as exc:
        if _is_closed_transport_runtime_error(exc):
            raise SSEClientDisconnected() from exc
        raise


async def _safe_write_eof(resp: web.StreamResponse) -> None:
    try:
        await resp.write_eof()
    except (ConnectionResetError, BrokenPipeError):
        return
    except RuntimeError as exc:
        if _is_closed_transport_runtime_error(exc):
            return
        raise


async def _wait_for_event_or_completion(sub_queue: asyncio.Queue, run_task: asyncio.Task, timeout: float) -> tuple[str, dict[str, Any] | None]:
    queue_task = asyncio.create_task(sub_queue.get())
    try:
        done, _ = await asyncio.wait({run_task, queue_task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if queue_task in done:
            return "event", queue_task.result()
        if run_task in done:
            return "completed", None
        return "timeout", None
    finally:
        if not queue_task.done():
            queue_task.cancel()
            await asyncio.gather(queue_task, return_exceptions=True)


def _event_dedupe_key(event: dict[str, Any]) -> tuple:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    raw_preview = json.dumps(data.get("raw_event_preview", {}), sort_keys=True, ensure_ascii=False)
    raw_hash = hashlib.sha256(raw_preview.encode("utf-8")).hexdigest()[:12] if raw_preview else ""
    return (event.get("type"), event.get("session_id"), event.get("request_id"), event.get("task_id"), event.get("tool"), event.get("permission_id"), event.get("raw_type"), data.get("status"), data.get("delta"), raw_hash)


def _is_stream_relevant_event(event: dict[str, Any], *, session_id: str, request_id: str) -> bool:
    if str(event.get("session_id") or "") != session_id:
        return False
    explicit_portal_req = event.get("portal_request_id")
    if not explicit_portal_req and isinstance(event.get("data"), dict):
        explicit_portal_req = event["data"].get("portal_request_id")
    if explicit_portal_req and str(explicit_portal_req) != request_id:
        return False
    event_type = str(event.get("type") or event.get("event_type") or "")
    if event_type in {"opencode.sync", "opencode.message.updated", "session.updated", "session.status", "session.idle", "session.diff"}:
        return False
    if event_type in BRIDGE_EVENT_TYPES or event_type.startswith("tool.") or event_type.startswith("permission_"):
        if event_type in {"assistant_delta", "message.delta"}:
            return bool(_event_delta_text(event))
        return True
    return False


def _event_delta_text(event: dict[str, Any]) -> str:
    for k in ("delta", "message", "text", "content"):
        v = event.get(k)
        if isinstance(v, str) and v:
            return safe_preview(v, 300)
    data = event.get("data")
    if isinstance(data, dict):
        for k in ("delta", "message", "text", "content"):
            v = data.get(k)
            if isinstance(v, str) and v:
                return safe_preview(v, 300)
    return ""


def _stream_delta_payload(event: dict[str, Any], delta: str, session_id: str, req_id: str) -> dict[str, Any]:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    return {
        "delta": delta,
        "session_id": session_id,
        "request_id": req_id,
        "raw_type": event.get("raw_type") or data.get("raw_type") or "",
        "message_role": data.get("message_role") or data.get("role") or event.get("message_role") or "",
        "part_type": data.get("part_type") or "",
        "message_id": data.get("message_id") or "",
        "part_id": data.get("part_id") or "",
    }


async def _stream_error_response(request: web.Request, error: str, detail: str | None = None) -> web.StreamResponse:
    resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache", "Connection": "keep-alive"})
    await resp.prepare(request)
    client_disconnected = False
    try:
        await _write_sse(resp, "error", {"error": error, "detail": detail or error})
    except SSEClientDisconnected:
        client_disconnected = True
    finally:
        if not client_disconnected:
            await _safe_write_eof(resp)
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
        return await _stream_error_response(request, "chat_failed", exc.text)

    payload = {**payload, "session_id": session_id, "request_id": req_id}
    resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache", "Connection": "keep-alive"})
    await resp.prepare(request)

    bus = request.app[EVENT_BUS_KEY]
    sub = bus.subscribe({"session_id": session_id})
    run_task = asyncio.create_task(handle_chat_payload(request, payload))
    seen: set[tuple] = set()
    settings = request.app[SETTINGS_KEY]
    stream_trace = build_trace_context(settings, request_id=req_id, session_id=session_id)
    client_disconnected = False
    sent_real_model_delta = False

    async def _forward(event: dict[str, Any]) -> None:
        if not _is_stream_relevant_event(event, session_id=session_id, request_id=req_id):
            return
        key = _event_dedupe_key(event)
        if key in seen:
            return
        seen.add(key)
        await _write_sse(resp, "runtime_event", event)
        nonlocal sent_real_model_delta
        if event.get("type") in {"assistant_delta", "message.delta"}:
            delta = _event_delta_text(event)
            if not delta:
                return
            raw_type = str(event.get("raw_type") or "").lower()
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            role = str(data.get("message_role") or data.get("role") or data.get("source_role") or event.get("role") or "").lower()
            is_real = (
                event.get("type") == "message.delta"
                and raw_type == "message.part.delta"
                and role == "assistant"
            )
            is_synth = event.get("type") == "assistant_delta" and bool(event.get("synthetic_final_delta") or (event.get("data") or {}).get("synthetic_final_delta"))
            if is_real:
                sent_real_model_delta = True
                await _write_sse(resp, "delta", _stream_delta_payload(event, delta, session_id, req_id))
            elif is_synth:
                if not sent_real_model_delta:
                    await _write_sse(resp, "delta", _stream_delta_payload(event, delta, session_id, req_id))
            else:
                if event.get("type") != "message.delta":
                    await _write_sse(resp, "delta", _stream_delta_payload(event, delta, session_id, req_id))

    try:
        await _write_sse(resp, "runtime_event", add_trace_context({"type": "stream.started", "engine": "opencode", "session_id": session_id, "request_id": req_id, "created_at": utc_now_iso()}, stream_trace))
        while not run_task.done():
            kind, event = await _wait_for_event_or_completion(sub.queue, run_task, STREAM_HEARTBEAT_SECONDS)
            if kind == "event" and event is not None:
                await _forward(event)
                continue
            if kind == "completed":
                break
            await _write_sse(resp, "heartbeat", {"ok": True, "ts": time.time()})

        error_payload = None
        final_result = None
        try:
            final_result = run_task.result()
        except web.HTTPException as exc:
            error_payload = {"error": "chat_failed", "detail": exc.text}
        except Exception as exc:
            error_payload = {"error": "chat_failed", "detail": safe_preview(str(exc), 500)}

        deadline = asyncio.get_running_loop().time() + 0.1
        drained = 0
        while asyncio.get_running_loop().time() < deadline and drained < 100:
            try:
                event = sub.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            await _forward(event); drained += 1

        if error_payload:
            await _write_sse(resp, "error", error_payload)
        else:
            await _write_sse(resp, "final", final_result or {})
            await _write_sse(resp, "done", {"ok": True})
    except SSEClientDisconnected:
        client_disconnected = True
    finally:
        bus.unsubscribe(sub)
        if not run_task.done():
            if not client_disconnected:
                run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
        elif client_disconnected:
            await asyncio.gather(run_task, return_exceptions=True)
        if not client_disconnected:
            await _safe_write_eof(resp)
    return resp
