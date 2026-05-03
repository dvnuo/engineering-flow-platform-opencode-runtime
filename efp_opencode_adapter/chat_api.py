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
    if not isinstance(payload, dict):
        return ""
    for key in ("id", "session_id", "uuid"):
        if payload.get(key):
            return str(payload[key])
    for key in ("session", "data", "info"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            nested_id = _extract_session_id(nested)
            if nested_id:
                return nested_id
    return ""


def _bad_request(error: str) -> web.HTTPBadRequest:
    return web.HTTPBadRequest(text=json.dumps({"error": error}), content_type="application/json")


def _metadata_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if "metadata" not in payload or payload.get("metadata") is None:
        return {}
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise _bad_request("metadata_must_be_object")
    return metadata


def _runtime_profile_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    if "runtime_profile" not in metadata or metadata.get("runtime_profile") is None:
        return {}
    runtime_profile = metadata.get("runtime_profile")
    if not isinstance(runtime_profile, dict):
        raise _bad_request("runtime_profile_must_be_object")
    return runtime_profile


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _normalize_title(raw: Any) -> str:
    text = re.sub(r"\s+", " ", raw if isinstance(raw, str) else "").strip()
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


def extract_assistant_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for k in ["response", "text", "content"]:
            if isinstance(payload.get(k), str) and payload.get(k):
                return payload[k]
        message = payload.get("message")
        if isinstance(message, dict):
            if _message_role(message) in (None, "assistant"):
                if isinstance(message.get("content"), str) and message.get("content"):
                    return message["content"]
                if isinstance(message.get("parts"), list):
                    text = message_parts_to_text(message["parts"])
                    if text:
                        return text
        msgs = payload.get("messages")
        if isinstance(msgs, list):
            return extract_assistant_text(msgs)
        if isinstance(payload.get("parts"), list) and _message_role(payload) in (None, "assistant"):
            return message_parts_to_text(payload["parts"])
    if isinstance(payload, list):
        for msg in reversed(payload):
            if _message_role(msg) == "assistant":
                text = extract_assistant_text(msg)
                if text:
                    return text
        for msg in reversed(payload):
            text = extract_assistant_text(msg)
            if text:
                return text
        return ""
    return ""


async def _publish_failed(
    bus,
    *,
    portal_session_id: str,
    request_id: str,
    opencode_session_id: str,
    error: str,
) -> None:
    await bus.publish(
        {
            "type": "execution.failed",
            "engine": "opencode",
            "session_id": portal_session_id,
            "request_id": request_id,
            "opencode_session_id": opencode_session_id,
            "timestamp": _utc_now_iso(),
            "error": error,
        }
    )


async def handle_chat_payload(request: web.Request, payload: dict[str, Any]) -> dict[str, Any]:
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        raise web.HTTPBadRequest(text=json.dumps({"error": "message_required"}), content_type="application/json")

    metadata = _metadata_from_payload(payload)
    runtime_profile = _runtime_profile_from_metadata(metadata)

    portal_session_id = payload.get("session_id") or str(uuid4())
    request_id = payload.get("request_id") or f"chat-{uuid4()}"
    title_source = _optional_str(metadata.get("title")) or _optional_str(metadata.get("name")) or message[:60]
    title = _normalize_title(title_source)
    model = _optional_str(metadata.get("model")) or _optional_str(runtime_profile.get("model"))
    agent = _optional_str(metadata.get("agent")) or _optional_str(runtime_profile.get("agent"))
    system = _optional_str(metadata.get("system")) or _optional_str(metadata.get("system_prompt"))

    store = request.app["session_store"]
    bus = request.app["event_bus"]
    client = request.app["opencode_client"]

    partial_recovery = False
    opencode_session_id_for_event = ""

    try:
        record = store.get(portal_session_id)
        if record is None:
            created = await client.create_session(title=title)
            opencode_session_id = _extract_session_id(created)
            if not opencode_session_id:
                raise OpenCodeClientError("create_session returned no session id", status=502, payload=created)
            opencode_session_id_for_event = opencode_session_id
            now = _utc_now_iso()
            record = SessionRecord(portal_session_id, opencode_session_id, title, agent, model, now, now, "", 0)
            store.upsert(record)
        else:
            opencode_session_id_for_event = record.opencode_session_id
            try:
                await client.get_session(record.opencode_session_id)
            except OpenCodeClientError as exc:
                if exc.status == 404:
                    created = await client.create_session(title=record.title)
                    opencode_session_id = _extract_session_id(created)
                    if not opencode_session_id:
                        raise OpenCodeClientError("create_session returned no session id", status=502, payload=created)
                    partial_recovery = True
                    opencode_session_id_for_event = opencode_session_id
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
        await _publish_failed(
            bus,
            portal_session_id=portal_session_id,
            request_id=request_id,
            opencode_session_id=opencode_session_id_for_event,
            error=str(exc),
        )
        raise web.HTTPBadGateway(
            text=json.dumps({"error": "opencode_error", "detail": str(exc)}),
            content_type="application/json",
        )

    out = {
        "session_id": portal_session_id,
        "request_id": request_id,
        "response": assistant_text,
        "events": [],
        "runtime_events": [],
        "usage": {},
        "context_state": {},
        "_llm_debug": {"engine": "opencode", "opencode_session_id": updated.opencode_session_id},
    }
    if partial_recovery or getattr(updated, "partial_recovery", False):
        out["_llm_debug"]["partial_recovery"] = True
    return out


async def chat_handler(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid_json"}), content_type="application/json")
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid_json"}), content_type="application/json")
    result = await handle_chat_payload(request, payload)
    return web.json_response(result)


async def chat_stream_handler(request: web.Request) -> web.StreamResponse:
    try:
        raw_payload = await request.json()
    except Exception:
        raw_payload = None

    payload = dict(raw_payload) if isinstance(raw_payload, dict) else {}
    payload.setdefault("request_id", f"chat-{uuid4()}")
    payload.setdefault("session_id", str(uuid4()))
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    await resp.prepare(request)
    runtime_evt = {
        "type": "execution.started",
        "session_id": payload.get("session_id"),
        "request_id": payload.get("request_id"),
        "engine": "opencode",
    }
    await resp.write(f"event: runtime_event\ndata: {json.dumps(runtime_evt, ensure_ascii=False)}\n\n".encode())
    try:
        if raw_payload is None:
            raise web.HTTPBadRequest(text=json.dumps({"error": "invalid_json"}), content_type="application/json")
        if not isinstance(raw_payload, dict):
            raise web.HTTPBadRequest(text=json.dumps({"error": "invalid_json"}), content_type="application/json")
        result = await handle_chat_payload(request, payload)
        await resp.write(f"event: final\ndata: {json.dumps(result, ensure_ascii=False)}\n\n".encode())
        await resp.write(b"event: done\ndata: {\"ok\":true}\n\n")
    except web.HTTPException as exc:
        detail = exc.text if exc.text else exc.reason
        await resp.write(
            f"event: error\ndata: {json.dumps({'error': 'chat_failed', 'detail': detail}, ensure_ascii=False)}\n\n".encode()
        )
    except Exception as exc:
        await resp.write(
            f"event: error\ndata: {json.dumps({'error': 'chat_failed', 'detail': str(exc)}, ensure_ascii=False)}\n\n".encode()
        )
    await resp.write_eof()
    return resp
