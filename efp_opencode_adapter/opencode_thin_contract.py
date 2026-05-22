from __future__ import annotations

from typing import Any

from aiohttp import web

from .app_keys import SETTINGS_KEY
from .opencode_binding_store import OpenCodeConversationBinding, binding_to_public
from .profile_store import sanitize_public_secrets


SOURCE_OF_TRUTH = "opencode"


def ok_payload(**values: Any) -> dict[str, Any]:
    return {"ok": True, "source_of_truth": SOURCE_OF_TRUTH, **values}


def error_payload(error: str, **values: Any) -> dict[str, Any]:
    return {"ok": False, "source_of_truth": SOURCE_OF_TRUTH, "error": error, **values}


async def read_json_object(request: web.Request) -> dict[str, Any]:
    try:
        value = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text='{"ok": false, "error": "invalid_json"}', content_type="application/json")
    if not isinstance(value, dict):
        raise web.HTTPBadRequest(text='{"ok": false, "error": "invalid_json"}', content_type="application/json")
    return value


def public_conversation(binding: OpenCodeConversationBinding, **extra: Any) -> dict[str, Any]:
    payload = binding_to_public(binding)
    payload.update(extra)
    return payload


def public_error_detail(exc: BaseException) -> str:
    detail = sanitize_public_secrets(str(exc).split("\n", 1)[0][:1000])
    return detail if isinstance(detail, str) else "opencode_error"


def extract_created_session_id(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("id", "session_id", "sessionID", "sessionId", "uuid"):
        item = value.get(key)
        if item:
            return str(item)
    for key in ("session", "data", "info"):
        nested = value.get(key)
        if isinstance(nested, dict):
            nested_id = extract_created_session_id(nested)
            if nested_id:
                return nested_id
    return ""


def request_agent_id(request: web.Request, body: dict[str, Any] | None = None) -> str:
    body = body or {}
    for value in (
        body.get("agent_id"),
        request.headers.get("X-Portal-Agent-Id"),
        request.headers.get("X-Agent-Id"),
        getattr(request.app[SETTINGS_KEY], "portal_agent_id", None),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "default"
