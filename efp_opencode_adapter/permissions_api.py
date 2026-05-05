from __future__ import annotations

import json

from aiohttp import web
from .app_keys import (
    EVENT_BUS_KEY,
    OPENCODE_CLIENT_KEY,
    PORTAL_METADATA_CLIENT_KEY,
    SESSION_STORE_KEY,
)

from .opencode_client import OpenCodeClientError
from .thinking_events import build_thinking_event


async def permission_respond_handler(request: web.Request) -> web.Response:
    permission_id = request.match_info["permission_id"]
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid_json"}), content_type="application/json")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid_json"}), content_type="application/json")
    decision = body.get("decision")
    if decision not in {"allow", "deny", "approve", "reject"}:
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid_decision"}), content_type="application/json")
    opencode_session_id = body.get("opencode_session_id")
    sid = body.get("session_id", "")
    if not opencode_session_id:
        rec = request.app[SESSION_STORE_KEY].get(sid)
        if rec is None:
            raise web.HTTPNotFound(text=json.dumps({"error": "session_not_found"}), content_type="application/json")
        opencode_session_id = rec.opencode_session_id
    payload = {"decision": decision, "remember": bool(body.get("remember", False))}
    try:
        await request.app[OPENCODE_CLIENT_KEY].respond_permission(opencode_session_id, permission_id, payload)
    except OpenCodeClientError as exc:
        raise web.HTTPBadGateway(text=json.dumps({"error": "opencode_error", "detail": str(exc)}), content_type="application/json")
    event = build_thinking_event("permission_resolved", session_id=str(sid or ""), request_id="", opencode_session_id=str(opencode_session_id), state="success", summary=f"Permission {decision}", data={"permission_id": permission_id, **payload})
    await request.app[EVENT_BUS_KEY].publish(event)

    portal_metadata_client = request.app.get(PORTAL_METADATA_CLIENT_KEY)
    if portal_metadata_client is not None:
        try:
            await portal_metadata_client.publish_session_metadata(
                session_id=str(sid or ""),
                latest_event_type="permission.resolved",
                latest_event_state="success",
                request_id="",
                summary=f"Permission {decision}",
                runtime_events=[event],
                metadata={
                    "engine": "opencode",
                    "opencode_session_id": str(opencode_session_id),
                    "permission_id": permission_id,
                    "decision": decision,
                },
            )
        except Exception:
            pass

    return web.json_response({"success": True})
