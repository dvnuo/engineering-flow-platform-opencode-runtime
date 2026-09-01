from __future__ import annotations

import json

from aiohttp import web
from .app_keys import (
    EVENT_BUS_KEY,
    OPENCODE_CLIENT_KEY,
    PENDING_INPUT_KEY,
    PORTAL_METADATA_CLIENT_KEY,
    SETTINGS_KEY,
    SESSION_STORE_KEY,
)

from .opencode_client import OpenCodeClientError, _permission_response_from_body
from .thinking_events import build_thinking_event
from .trace_context import add_trace_context, build_trace_context

_ALLOWED_DECISIONS = {"allow", "deny", "approve", "reject"}
_ALLOWED_PERMISSION_RESPONSES = {"once", "always", "reject"}


def _validate_permission_response_body(body: dict) -> tuple[str, dict[str, str]]:
    response = body.get("response")
    decision = body.get("decision")

    if isinstance(response, str) and response in _ALLOWED_PERMISSION_RESPONSES:
        return response, {"response": response}

    if isinstance(response, str) and response and response not in _ALLOWED_PERMISSION_RESPONSES:
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid_response"}), content_type="application/json")

    if decision not in _ALLOWED_DECISIONS:
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid_decision"}), content_type="application/json")

    payload = _permission_response_from_body(body)
    return str(decision), payload


async def permission_respond_handler(request: web.Request) -> web.Response:
    permission_id = request.match_info["permission_id"]
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid_json"}), content_type="application/json")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid_json"}), content_type="application/json")
    decision_or_response, payload = _validate_permission_response_body(body)
    request_id = body.get("request_id", "")
    if not isinstance(request_id, str):
        request_id = ""
    opencode_session_id = body.get("opencode_session_id")
    sid = body.get("session_id", "")
    if not isinstance(sid, str):
        sid = ""
    tool_name = body.get("tool", "")
    if not isinstance(tool_name, str):
        tool_name = ""
    if not opencode_session_id:
        rec = request.app[SESSION_STORE_KEY].get(sid)
        if rec is None:
            raise web.HTTPNotFound(text=json.dumps({"error": "session_not_found"}), content_type="application/json")
        opencode_session_id = rec.opencode_session_id
    try:
        await request.app[OPENCODE_CLIENT_KEY].respond_permission(opencode_session_id, permission_id, payload)
    except OpenCodeClientError as exc:
        raise web.HTTPBadGateway(text=json.dumps({"error": "opencode_error", "detail": str(exc)}), content_type="application/json")
    trace_context = build_trace_context(request.app[SETTINGS_KEY], request_id=request_id, session_id=sid, opencode_session_id=str(opencode_session_id or ""), tool_name=tool_name)
    event = add_trace_context(build_thinking_event("permission_resolved", session_id=str(sid or ""), request_id=request_id, opencode_session_id=str(opencode_session_id), state="success", summary=f"Permission {decision_or_response}", data={"permission_id": permission_id, **payload}), trace_context)
    await request.app[EVENT_BUS_KEY].publish(event)

    portal_metadata_client = request.app.get(PORTAL_METADATA_CLIENT_KEY)
    if portal_metadata_client is not None:
        try:
            await portal_metadata_client.publish_session_metadata(
                session_id=str(sid or ""),
                latest_event_type="permission.resolved",
                latest_event_state="success",
                request_id=request_id,
                summary=f"Permission {decision_or_response}",
                runtime_events=[event],
                metadata={
                    "engine": "opencode",
                    "opencode_session_id": str(opencode_session_id),
                    "permission_id": permission_id,
                    "decision": body.get("decision", ""),
                    "response": payload.get("response", ""),
                    "trace_context": trace_context,
                },
            )
        except Exception:
            pass

    return web.json_response({"success": True})


# --------------------------------------------------------- session-scoped API
#
# Portal drives its approval card off the session, not the permission id: it
# polls `/api/sessions/{id}/pending-input` to rebuild a card after a refresh and
# posts the answer to `/api/sessions/{id}/permission/respond`. The native
# runtime serves both. The routes below are the same contract over OpenCode's
# permission model, so Portal never has to branch on runtime type.

_APPROVE_WORDS = {"approve", "approved", "allow", "allowed", "accept", "accepted"}
_DENY_WORDS = {"deny", "denied", "reject", "rejected"}


def _json_error(message: str, status: int) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _normalized_decision(value) -> str:
    """approve/deny, or ValueError. A typo must not silently deny a tool."""

    normalized = str(value or "").strip().lower()
    if normalized in _APPROVE_WORDS:
        return "approve"
    if normalized in _DENY_WORDS:
        return "deny"
    raise ValueError("decision must be approve or deny")


# Only these mean "yes". `bool(value)` would read the *string* "false" -- and
# "0", and any other stray text -- as true, and the true direction is the
# dangerous one: it persists a standing approval for that tool in the session
# rather than approving this one call. Anything unrecognised falls back to a
# single-use approval.
_TRUE_WORDS = {"true", "yes", "on", "1"}


def _wants_standing_approval(body: dict) -> bool:
    for key in ("always", "remember"):
        value = body.get(key)
        if value is True:
            return True
        if isinstance(value, str) and value.strip().lower() in _TRUE_WORDS:
            return True
        if isinstance(value, int) and not isinstance(value, bool) and value == 1:
            return True
    return False


def _opencode_response_for(decision: str, *, always: bool) -> str:
    if decision == "deny":
        return "reject"
    return "always" if always else "once"


async def _read_json_object(request: web.Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid_json"}), content_type="application/json")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid_json"}), content_type="application/json")
    return body


async def session_pending_input_handler(request: web.Request) -> web.Response:
    """GET /api/sessions/{session_id}/pending-input"""

    session_id = str(request.match_info.get("session_id") or "").strip()
    if not session_id:
        return _json_error("session_id is required", 400)
    return web.json_response(request.app[PENDING_INPUT_KEY].snapshot(session_id))


async def session_question_respond_handler(request: web.Request) -> web.Response:
    """POST /api/sessions/{session_id}/question/respond

    OpenCode 1.14.x exposes no route to deliver a typed answer back to a
    `question` tool call -- its full server surface offers only
    `/session/{id}/permissions/{permissionID}`. Answering is therefore not
    something this adapter can fake, and saying so plainly beats a 404 that
    reads like a deployment fault.
    """
    return web.json_response(
        {
            "error": "question_response_unsupported",
            "detail": (
                "The OpenCode runtime has no question-response API. "
                "Questions cannot be answered on this runtime; use the native runtime "
                "for assistants that rely on the question tool."
            ),
            "engine": "opencode",
        },
        status=501,
    )


async def session_permission_respond_handler(request: web.Request) -> web.Response:
    """POST /api/sessions/{session_id}/permission/respond"""

    session_id = str(request.match_info.get("session_id") or "").strip()
    if not session_id:
        return _json_error("session_id is required", 400)
    body = await _read_json_object(request)

    pending_store = request.app[PENDING_INPUT_KEY]
    pending = pending_store.get(session_id)
    if pending is None:
        return _json_error("Session is not waiting for permission", 409)

    requested_id = str(body.get("request_id") or body.get("id") or "").strip()
    if requested_id and requested_id != pending.permission_id:
        return _json_error("permission_request_id_mismatch", 409)

    try:
        decision = _normalized_decision(body.get("decision") or body.get("action") or body.get("reply"))
    except ValueError as exc:
        return _json_error(str(exc), 400)
    response = _opencode_response_for(decision, always=_wants_standing_approval(body))

    # Claimed before the call, not after: two clicks landing together would
    # otherwise both pass the checks above and approve the same tool twice.
    if not pending_store.claim(session_id, pending.permission_id):
        return _json_error("permission_already_answered", 409)

    opencode_session_id = pending.opencode_session_id
    if not opencode_session_id:
        record = request.app[SESSION_STORE_KEY].get(session_id)
        if record is None:
            pending_store.release(session_id, pending.permission_id)
            return _json_error("session_not_found", 404)
        opencode_session_id = record.opencode_session_id

    try:
        await request.app[OPENCODE_CLIENT_KEY].respond_permission(
            opencode_session_id, pending.permission_id, {"response": response}
        )
    except OpenCodeClientError as exc:
        # The permission is still waiting, so the member must be able to try
        # again rather than face a card whose buttons are now inert.
        pending_store.release(session_id, pending.permission_id)
        raise web.HTTPBadGateway(
            text=json.dumps({"error": "opencode_error", "detail": str(exc)}), content_type="application/json"
        )
    except Exception:
        pending_store.release(session_id, pending.permission_id)
        raise

    pending_store.clear(session_id, permission_id=pending.permission_id)
    await _publish_permission_resolved(
        request,
        session_id=session_id,
        opencode_session_id=str(opencode_session_id or ""),
        permission_id=pending.permission_id,
        request_id=pending.request_id,
        tool_name=str(pending.request.get("tool") or ""),
        decision=decision,
        payload={"response": response},
    )
    return web.json_response(
        {
            "ok": True,
            "session_id": session_id,
            "request_id": pending.permission_id,
            "decision": decision,
            "response": response,
            "state": "running",
        },
        status=202,
    )


async def _publish_permission_resolved(
    request: web.Request,
    *,
    session_id: str,
    opencode_session_id: str,
    permission_id: str,
    request_id: str,
    tool_name: str,
    decision: str,
    payload: dict,
) -> None:
    """Announce the resolution so the card clears everywhere it was drawn."""

    trace_context = build_trace_context(
        request.app[SETTINGS_KEY],
        request_id=request_id,
        session_id=session_id,
        opencode_session_id=opencode_session_id,
        tool_name=tool_name,
    )
    event = add_trace_context(
        build_thinking_event(
            "permission_resolved",
            session_id=session_id,
            request_id=request_id,
            opencode_session_id=opencode_session_id,
            state="success",
            summary=f"Permission {decision}",
            data={"permission_id": permission_id, **payload},
        ),
        trace_context,
    )
    await request.app[EVENT_BUS_KEY].publish(event)

    portal_metadata_client = request.app.get(PORTAL_METADATA_CLIENT_KEY)
    if portal_metadata_client is None:
        return
    try:
        await portal_metadata_client.publish_session_metadata(
            session_id=session_id,
            latest_event_type="permission.resolved",
            latest_event_state="success",
            request_id=request_id,
            summary=f"Permission {decision}",
            runtime_events=[event],
            metadata={
                "engine": "opencode",
                "opencode_session_id": opencode_session_id,
                "permission_id": permission_id,
                "decision": decision,
                "response": payload.get("response", ""),
                "trace_context": trace_context,
            },
        )
    except Exception:
        pass
