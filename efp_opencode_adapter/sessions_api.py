from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from aiohttp import web

from .opencode_client import OpenCodeClientError


def _message_info(message: Any) -> dict[str, Any]:
    if isinstance(message, dict) and isinstance(message.get("info"), dict):
        return message["info"]
    if isinstance(message, dict):
        return message
    return {}


def _timestamp_from_message(message: dict[str, Any], info: dict[str, Any]) -> str:
    for key in ("timestamp", "created_at", "updated_at"):
        if isinstance(message.get(key), str) and message.get(key):
            return message[key]

    time_info = info.get("time")
    if isinstance(time_info, dict):
        created = time_info.get("created")
        if isinstance(created, (int, float)):
            seconds = created / 1000 if created > 1_000_000_000_000 else created
            return datetime.fromtimestamp(seconds, UTC).isoformat()

    return ""


def message_to_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if not isinstance(message, dict):
        return json.dumps(message, ensure_ascii=False)

    content = message.get("content")
    if isinstance(content, str):
        return content

    nested_message = message.get("message") if isinstance(message.get("message"), dict) else None
    parts = message.get("parts")
    if not isinstance(parts, list) and nested_message is not None:
        parts = nested_message.get("parts")

    if isinstance(parts, list):
        out = []
        for part in parts:
            if isinstance(part, dict):
                if part.get("type") == "text" and part.get("text"):
                    out.append(str(part["text"]))
                elif part.get("content"):
                    out.append(str(part.get("content")))
                else:
                    out.append(json.dumps(part, ensure_ascii=False))
            else:
                out.append(json.dumps(part, ensure_ascii=False))
        return "\n".join(out)
    return ""


def _to_efp_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for idx, msg in enumerate(messages):
        info = _message_info(msg)
        out.append(
            {
                "id": str(info.get("id") or msg.get("id") or msg.get("message_id") or idx),
                "role": str(info.get("role") or msg.get("role") or "unknown"),
                "content": message_to_text(msg),
                "timestamp": _timestamp_from_message(msg, info),
            }
        )
    return out


async def list_sessions_handler(request: web.Request) -> web.Response:
    store = request.app["session_store"]
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
    store = request.app["session_store"]
    client = request.app["opencode_client"]
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
    detail = await get_session_handler(request)
    payload = json.loads(detail.text)
    return web.json_response(
        {"session_id": payload["session_id"], "messages": payload["messages"], "metadata": {"engine": "opencode"}}
    )


async def rename_session_handler(request: web.Request) -> web.Response:
    sid = request.match_info["session_id"]
    store = request.app["session_store"]
    client = request.app["opencode_client"]
    record = store.get(sid)
    if not record or record.deleted:
        raise web.HTTPNotFound(text=json.dumps({"error": "session_not_found"}), content_type="application/json")
    body = await request.json()
    title = (body.get("name") or body.get("title") or "").strip()
    if not title:
        raise web.HTTPBadRequest(text=json.dumps({"error": "title_required"}), content_type="application/json")
    updated = store.rename(sid, title)
    try:
        await client.patch_session(updated.opencode_session_id, title)
    except OpenCodeClientError as exc:
        if exc.status != 404:
            raise web.HTTPBadGateway(text=json.dumps({"error": "opencode_error", "detail": str(exc)}), content_type="application/json")
    return web.json_response({"success": True, "session_id": sid, "name": title})


async def delete_session_handler(request: web.Request) -> web.Response:
    sid = request.match_info["session_id"]
    store = request.app["session_store"]
    client = request.app["opencode_client"]
    record = store.mark_deleted(sid)
    if record:
        try:
            await client.delete_session(record.opencode_session_id)
        except OpenCodeClientError as exc:
            if exc.status != 404:
                pass
    return web.json_response({"success": True})


async def clear_sessions_handler(request: web.Request) -> web.Response:
    store = request.app["session_store"]
    client = request.app["opencode_client"]
    cleared = store.clear()
    for rec in cleared:
        try:
            await client.delete_session(rec.opencode_session_id)
        except OpenCodeClientError:
            pass
    return web.json_response({"success": True, "cleared": len(cleared)})


async def unsupported_message_mutation_handler(request: web.Request) -> web.Response:
    return web.json_response({"success": False, "error": "unsupported_by_opencode_adapter_mvp"}, status=501)
