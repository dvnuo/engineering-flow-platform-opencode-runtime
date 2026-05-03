from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from aiohttp import web

from .opencode_client import OpenCodeClientError
from .session_store import SessionRecord


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _extract_session_id(payload: dict[str, Any]) -> str:
    return payload.get("id") or payload.get("session_id") or payload.get("uuid") or ""


def _normalize_title(raw: str) -> str:
    text = re.sub(r"\s+", " ", (raw or "")).strip()
    return text or "Chat"


def message_parts_to_text(parts: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for part in parts:
        if part.get("type") == "text" and part.get("text"):
            out.append(str(part.get("text")))
        elif part.get("content"):
            out.append(str(part.get("content")))
        else:
            out.append(json.dumps(part, ensure_ascii=False))
    return "\n".join(x for x in out if x)


def extract_assistant_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for k in ["response", "text", "content"]:
            if isinstance(payload.get(k), str) and payload.get(k):
                return payload[k]
        message = payload.get("message")
        if isinstance(message, dict):
            if isinstance(message.get("content"), str) and message.get("content"):
                return message["content"]
            if isinstance(message.get("parts"), list):
                return message_parts_to_text(message["parts"])
        msgs = payload.get("messages")
        if isinstance(msgs, list):
            return extract_assistant_text(msgs)
        if isinstance(payload.get("parts"), list):
            return message_parts_to_text(payload["parts"])
    if isinstance(payload, list):
        for msg in reversed(payload):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                return extract_assistant_text(msg)
        if payload:
            return extract_assistant_text(payload[-1])
    return ""


async def handle_chat_payload(request: web.Request, payload: dict[str, Any]) -> dict[str, Any]:
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        raise web.HTTPBadRequest(text=json.dumps({"error": "message_required"}), content_type="application/json")

    metadata = payload.get("metadata") or {}
    portal_session_id = payload.get("session_id") or str(uuid4())
    request_id = payload.get("request_id") or f"chat-{uuid4()}"
    title = _normalize_title(metadata.get("title") or metadata.get("name") or message[:60])
    model = metadata.get("model") or (metadata.get("runtime_profile") or {}).get("model")
    agent = metadata.get("agent") or (metadata.get("runtime_profile") or {}).get("agent")
    system = metadata.get("system") or metadata.get("system_prompt")

    store = request.app["session_store"]
    bus = request.app["event_bus"]
    client = request.app["opencode_client"]

    partial_recovery = False
    record = store.get(portal_session_id)
    if record is None:
        created = await client.create_session(title=title)
        opencode_session_id = _extract_session_id(created)
        now = _utc_now_iso()
        record = SessionRecord(portal_session_id, opencode_session_id, title, agent, model, now, now, "", 0)
        store.upsert(record)
    else:
        try:
            await client.get_session(record.opencode_session_id)
        except OpenCodeClientError as exc:
            if exc.status == 404:
                created = await client.create_session(title=record.title)
                opencode_session_id = _extract_session_id(created)
                partial_recovery = True
                record = SessionRecord(
                    **{
                        **record.__dict__,
                        "opencode_session_id": opencode_session_id,
                        "partial_recovery": True,
                        "updated_at": _utc_now_iso(),
                    }
                )
                store.upsert(record)
            else:
                raise

    started_event = {
        "type": "execution.started",
        "engine": "opencode",
        "session_id": portal_session_id,
        "request_id": request_id,
        "opencode_session_id": record.opencode_session_id,
        "timestamp": _utc_now_iso(),
    }
    await bus.publish(started_event)

    try:
        response_payload = await client.send_message(
            record.opencode_session_id,
            parts=[{"type": "text", "text": message}],
            model=model,
            agent=agent,
            system=system,
        )
        assistant_text = extract_assistant_text(response_payload) or "[no assistant response]"
        updated = store.update_after_chat(portal_session_id, message, assistant_text, model, agent)
        completed_event = {
            "type": "execution.completed",
            "engine": "opencode",
            "session_id": portal_session_id,
            "request_id": request_id,
            "opencode_session_id": updated.opencode_session_id,
            "timestamp": _utc_now_iso(),
            "summary": assistant_text[:120],
        }
        await bus.publish(completed_event)
    except OpenCodeClientError as exc:
        await bus.publish(
            {
                "type": "execution.failed",
                "engine": "opencode",
                "session_id": portal_session_id,
                "request_id": request_id,
                "opencode_session_id": record.opencode_session_id,
                "timestamp": _utc_now_iso(),
                "error": str(exc),
            }
        )
        raise web.HTTPBadGateway(text=json.dumps({"error": "opencode_error", "detail": str(exc)}), content_type="application/json")

    out = {
        "session_id": portal_session_id,
        "request_id": request_id,
        "response": assistant_text,
        "events": [],
        "runtime_events": [],
        "usage": {},
        "context_state": {},
        "_llm_debug": {"engine": "opencode", "opencode_session_id": record.opencode_session_id},
    }
    if partial_recovery or getattr(updated, "partial_recovery", False):
        out["_llm_debug"]["partial_recovery"] = True
    return out


async def chat_handler(request: web.Request) -> web.Response:
    payload = await request.json()
    result = await handle_chat_payload(request, payload)
    return web.json_response(result)


async def chat_stream_handler(request: web.Request) -> web.StreamResponse:
    payload = await request.json()
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    await resp.prepare(request)
    try:
        result = await handle_chat_payload(request, payload)
        runtime_evt = {
            "type": "execution.started",
            "session_id": result.get("session_id"),
            "request_id": result.get("request_id"),
            "engine": "opencode",
        }
        await resp.write(f"event: runtime_event\ndata: {json.dumps(runtime_evt, ensure_ascii=False)}\n\n".encode())
        await resp.write(f"event: final\ndata: {json.dumps(result, ensure_ascii=False)}\n\n".encode())
        await resp.write(b"event: done\ndata: {\"ok\":true}\n\n")
    except web.HTTPException as exc:
        detail = exc.text if exc.text else exc.reason
        await resp.write(f"event: error\ndata: {json.dumps({'error': 'chat_failed', 'detail': detail}, ensure_ascii=False)}\n\n".encode())
    await resp.write_eof()
    return resp
