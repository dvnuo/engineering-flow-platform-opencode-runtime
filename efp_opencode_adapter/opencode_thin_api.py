from __future__ import annotations

import json
from typing import Any

from aiohttp import web

from .app_keys import OPENCODE_BINDING_STORE_KEY, OPENCODE_CLIENT_KEY
from .opencode_binding_store import OpenCodeBindingStore
from .opencode_client import OpenCodeClientError, _model_ref_from_value
from .opencode_permission_adapter import map_permission_response
from .opencode_status_adapter import build_conversation_status, unreachable_status
from .opencode_thin_contract import (
    error_payload,
    extract_created_session_id,
    ok_payload,
    public_conversation,
    public_error_detail,
    read_json_object,
    request_agent_id,
)
from .thinking_events import safe_preview


def _store(request: web.Request) -> OpenCodeBindingStore:
    return request.app[OPENCODE_BINDING_STORE_KEY]


def _client(request: web.Request):
    return request.app[OPENCODE_CLIENT_KEY]


def _conversation_not_found() -> web.Response:
    return web.json_response(error_payload("conversation_not_found"), status=404)


def _binding_for_request(request: web.Request):
    binding = _store(request).get(request.match_info["conversation_id"])
    if binding is None or binding.archived_at:
        return None
    return binding


async def _create_opencode_session(client, *, title: str, parent_id: str | None = None) -> dict[str, Any]:
    try:
        return await client.create_session(title=title or None, parent_id=parent_id)
    except TypeError:
        return await client.create_session(title=title or None)


async def _client_children(client, session_id: str) -> list[dict[str, Any]]:
    if hasattr(client, "children"):
        return await client.children(session_id)
    if hasattr(client, "list_session_children"):
        return await client.list_session_children(session_id)
    return []


async def _status_for_session(client, session_id: str, *, include_children: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        raw_status = await client.get_session_status()
    except Exception:
        return {}, unreachable_status()
    children: list[dict[str, Any]] = []
    if include_children:
        try:
            children = await _client_children(client, session_id)
        except Exception:
            children = []
    return raw_status if isinstance(raw_status, dict) else {"data": raw_status}, build_conversation_status(raw_status, session_id, children=children)


async def opencode_health_handler(request: web.Request) -> web.Response:
    try:
        info = await _client(request).health()
    except Exception:
        info = {"healthy": False}
    healthy = bool(info.get("healthy"))
    if healthy:
        return web.json_response(
            ok_payload(
                runtime={"healthy": True, "version": info.get("version")},
                opencode={"connected": True},
            )
        )
    return web.json_response(
        error_payload(
            "opencode_unreachable",
            runtime={"healthy": False},
            opencode={"connected": False},
        ),
        status=503,
    )


async def create_conversation_handler(request: web.Request) -> web.Response:
    body = await read_json_object(request)
    title = str(body.get("title") or "New chat")
    parent_id = body.get("parent_conversation_id")
    parent_opencode_session_id: str | None = None
    if parent_id:
        parent = _store(request).get(str(parent_id))
        if parent is None or parent.archived_at:
            return _conversation_not_found()
        parent_opencode_session_id = parent.opencode_session_id
    try:
        session = await _create_opencode_session(_client(request), title=title, parent_id=parent_opencode_session_id)
    except OpenCodeClientError as exc:
        return web.json_response(error_payload("opencode_error", detail=public_error_detail(exc)), status=502)
    opencode_session_id = extract_created_session_id(session)
    if not opencode_session_id:
        return web.json_response(error_payload("opencode_create_session_missing_id"), status=502)
    binding = _store(request).create(request_agent_id(request, body), opencode_session_id, title=title)
    return web.json_response(ok_payload(conversation=public_conversation(binding)))


async def list_conversations_handler(request: web.Request) -> web.Response:
    agent_id = str(request.query.get("agent_id") or request_agent_id(request))
    include_archived = str(request.query.get("include_archived") or "").lower() in {"1", "true", "yes", "on"}
    bindings = _store(request).list(agent_id, include_archived=include_archived)
    try:
        raw_status = await _client(request).get_session_status()
        unreachable = False
    except Exception:
        raw_status = {}
        unreachable = True
    conversations = []
    for binding in bindings:
        if unreachable:
            status = unreachable_status()
        else:
            status = build_conversation_status(raw_status, binding.opencode_session_id, children=[])
        conversations.append(public_conversation(binding, status=status["status"]))
    return web.json_response(ok_payload(conversations=conversations))


async def get_conversation_handler(request: web.Request) -> web.Response:
    binding = _binding_for_request(request)
    if binding is None:
        return _conversation_not_found()
    raw_status, status = await _status_for_session(_client(request), binding.opencode_session_id)
    return web.json_response(
        ok_payload(
            conversation=public_conversation(binding, status=status["status"]),
            raw_status=safe_preview(raw_status, 4000),
        )
    )


async def patch_conversation_handler(request: web.Request) -> web.Response:
    binding = _binding_for_request(request)
    if binding is None:
        return _conversation_not_found()
    body = await read_json_object(request)
    title = str(body.get("title") or "").strip()
    if not title:
        return web.json_response(error_payload("title_required"), status=400)
    try:
        if hasattr(_client(request), "patch_session"):
            await _client(request).patch_session(binding.opencode_session_id, title)
    except OpenCodeClientError as exc:
        return web.json_response(error_payload("opencode_error", detail=public_error_detail(exc)), status=502)
    updated = _store(request).update_title(binding.portal_conversation_id, title)
    return web.json_response(ok_payload(conversation=public_conversation(updated)))


async def delete_conversation_handler(request: web.Request) -> web.Response:
    binding = _binding_for_request(request)
    if binding is None:
        return _conversation_not_found()
    archived = _store(request).archive(binding.portal_conversation_id)
    return web.json_response(ok_payload(conversation=public_conversation(archived)))


async def conversation_status_handler(request: web.Request) -> web.Response:
    binding = _binding_for_request(request)
    if binding is None:
        return _conversation_not_found()
    raw_status, status = await _status_for_session(_client(request), binding.opencode_session_id)
    return web.json_response(
        ok_payload(
            conversation_id=binding.portal_conversation_id,
            opencode_session_id=binding.opencode_session_id,
            status=status["status"],
            children=status["children"],
            raw_status=safe_preview(raw_status, 4000),
        )
    )


async def conversation_messages_handler(request: web.Request) -> web.Response:
    binding = _binding_for_request(request)
    if binding is None:
        return _conversation_not_found()
    raw_limit = request.query.get("limit")
    limit = None
    if raw_limit:
        try:
            limit = max(0, int(raw_limit))
        except ValueError:
            return web.json_response(error_payload("invalid_limit"), status=400)
    try:
        try:
            messages = await _client(request).list_messages(binding.opencode_session_id, limit=limit)
        except TypeError:
            messages = await _client(request).list_messages(binding.opencode_session_id)
    except OpenCodeClientError as exc:
        return web.json_response(error_payload("opencode_error", detail=public_error_detail(exc)), status=502)
    _raw_status, status = await _status_for_session(_client(request), binding.opencode_session_id, include_children=False)
    return web.json_response(
        ok_payload(
            conversation_id=binding.portal_conversation_id,
            opencode_session_id=binding.opencode_session_id,
            messages=messages if isinstance(messages, list) else [],
            status=status["status"],
        )
    )


def _send_parts_from_body(body: dict[str, Any]) -> list[dict[str, Any]] | None:
    parts = body.get("parts")
    if isinstance(parts, list):
        return [part for part in parts if isinstance(part, dict)]
    text = body.get("text")
    if isinstance(text, str) and text:
        return [{"type": "text", "text": text}]
    return None


async def send_conversation_handler(request: web.Request) -> web.Response:
    binding = _binding_for_request(request)
    if binding is None:
        return _conversation_not_found()
    body = await read_json_object(request)
    attachments = body.get("attachments")
    if isinstance(attachments, list) and attachments:
        return web.json_response(
            error_payload(
                "attachments_unsupported_for_thin_send",
                action_hint="send_without_attachments_or_use_file_context",
            ),
            status=400,
        )
    raw_status, status = await _status_for_session(_client(request), binding.opencode_session_id, include_children=False)
    if status["status"]["active"]:
        return web.json_response(
            error_payload(
                "opencode_session_busy",
                status=status["status"],
                action_hint="wait_or_stop",
            ),
            status=409,
        )
    if not status["status"]["can_send"]:
        return web.json_response(
            error_payload(
                "opencode_status_unavailable",
                status=status["status"],
                raw_status=safe_preview(raw_status, 4000),
                action_hint="refresh_status",
            ),
            status=503,
        )
    parts = _send_parts_from_body(body)
    if not parts:
        return web.json_response(error_payload("message_required"), status=400)
    prompt_body: dict[str, Any] = {"parts": parts}
    message_id = body.get("message_id") or body.get("messageID")
    if message_id:
        prompt_body["messageID"] = str(message_id)
    if body.get("model") is not None:
        model_ref = _model_ref_from_value(body.get("model"))
        if model_ref:
            prompt_body["model"] = model_ref
    if body.get("agent") is not None:
        prompt_body["agent"] = body.get("agent")
    if isinstance(body.get("system"), str) and body.get("system").strip():
        prompt_body["system"] = body.get("system")
    if "tools" in body and isinstance(body.get("tools"), dict):
        prompt_body["tools"] = body.get("tools")
    if "noReply" in body or "no_reply" in body:
        prompt_body["noReply"] = bool(body.get("noReply", body.get("no_reply")))
    client = _client(request)
    try:
        if hasattr(client, "message"):
            response_payload = await client.message(binding.opencode_session_id, prompt_body)
        else:
            model_arg = None
            raw_model = body.get("model")
            if isinstance(raw_model, str):
                model_arg = raw_model
            elif isinstance(prompt_body.get("model"), dict):
                model_ref = prompt_body["model"]
                model_arg = f"{model_ref.get('providerID')}/{model_ref.get('modelID')}"
            response_payload = await client.send_message(
                binding.opencode_session_id,
                parts=prompt_body["parts"],
                model=model_arg,
                agent=prompt_body.get("agent"),
                system=prompt_body.get("system"),
                message_id=prompt_body.get("messageID"),
                no_reply=prompt_body.get("noReply"),
                tools=prompt_body.get("tools"),
            )
    except OpenCodeClientError as exc:
        return web.json_response(error_payload("opencode_error", detail=public_error_detail(exc)), status=502)
    except ValueError as exc:
        return web.json_response(error_payload("invalid_message_id", detail=str(exc)), status=400)
    try:
        messages = await client.list_messages(binding.opencode_session_id)
    except OpenCodeClientError as exc:
        return web.json_response(error_payload("opencode_error", detail=public_error_detail(exc)), status=502)
    _raw_status, post_status = await _status_for_session(client, binding.opencode_session_id, include_children=False)
    return web.json_response(
        ok_payload(
            status="completed",
            conversation_id=binding.portal_conversation_id,
            opencode_session_id=binding.opencode_session_id,
            message_id=message_id,
            message=response_payload,
            messages=messages if isinstance(messages, list) else [],
            session_status=post_status["status"],
            action_hint="refresh_messages",
        )
    )


async def _post_abort_status(client, session_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    return await _status_for_session(client, session_id, include_children=False)


async def abort_conversation_handler(request: web.Request) -> web.Response:
    binding = _binding_for_request(request)
    if binding is None:
        return _conversation_not_found()
    try:
        await _client(request).abort_session(binding.opencode_session_id)
    except OpenCodeClientError as exc:
        return web.json_response(error_payload("opencode_error", detail=public_error_detail(exc)), status=502)
    _raw_status, status = await _post_abort_status(_client(request), binding.opencode_session_id)
    if status["status"]["active"]:
        return web.json_response(
            error_payload(
                "opencode_abort_still_active",
                status=status["status"],
                actions=["retry_abort", "new_conversation"],
            ),
            status=409,
        )
    return web.json_response(
        ok_payload(
            conversation_id=binding.portal_conversation_id,
            opencode_session_id=binding.opencode_session_id,
            status=status["status"],
        )
    )


async def conversation_events_handler(request: web.Request) -> web.StreamResponse:
    binding = _binding_for_request(request)
    if binding is None:
        raise web.HTTPNotFound(text='{"ok": false, "error": "conversation_not_found"}', content_type="application/json")
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "Connection": "close",
        },
    )
    await response.prepare(request)
    for event_name, payload in (
        (
            "opencode.connected",
            {
                "ok": True,
                "conversation_id": binding.portal_conversation_id,
                "opencode_session_id": binding.opencode_session_id,
            },
        ),
        (
            "opencode.snapshot.required",
            {
                "ok": True,
                "conversation_id": binding.portal_conversation_id,
                "opencode_session_id": binding.opencode_session_id,
                "action_hint": "refresh_messages",
            },
        ),
    ):
        await response.write(f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
    await response.write_eof()
    return response


async def conversation_children_handler(request: web.Request) -> web.Response:
    binding = _binding_for_request(request)
    if binding is None:
        return _conversation_not_found()
    try:
        children = await _client_children(_client(request), binding.opencode_session_id)
    except OpenCodeClientError as exc:
        return web.json_response(error_payload("opencode_error", detail=public_error_detail(exc)), status=502)
    return web.json_response(ok_payload(conversation_id=binding.portal_conversation_id, opencode_session_id=binding.opencode_session_id, children=children))


async def conversation_todo_handler(request: web.Request) -> web.Response:
    binding = _binding_for_request(request)
    if binding is None:
        return _conversation_not_found()
    try:
        todo = await _client(request).todo(binding.opencode_session_id)
    except OpenCodeClientError as exc:
        return web.json_response(error_payload("opencode_error", detail=public_error_detail(exc)), status=502)
    return web.json_response(ok_payload(conversation_id=binding.portal_conversation_id, opencode_session_id=binding.opencode_session_id, todo=todo))


async def conversation_diff_handler(request: web.Request) -> web.Response:
    binding = _binding_for_request(request)
    if binding is None:
        return _conversation_not_found()
    message_id = request.query.get("message_id") or request.query.get("messageID")
    try:
        diff = await _client(request).diff(binding.opencode_session_id, message_id=message_id)
    except OpenCodeClientError as exc:
        return web.json_response(error_payload("opencode_error", detail=public_error_detail(exc)), status=502)
    return web.json_response(ok_payload(conversation_id=binding.portal_conversation_id, opencode_session_id=binding.opencode_session_id, diff=diff))


async def conversation_fork_handler(request: web.Request) -> web.Response:
    binding = _binding_for_request(request)
    if binding is None:
        return _conversation_not_found()
    body = await read_json_object(request)
    message_id = body.get("message_id") or body.get("messageID")
    try:
        if hasattr(_client(request), "fork"):
            forked = await _client(request).fork(binding.opencode_session_id, message_id=message_id)
        else:
            forked = await _client(request).fork_session(binding.opencode_session_id, message_id=message_id)
    except OpenCodeClientError as exc:
        return web.json_response(error_payload("opencode_error", detail=public_error_detail(exc)), status=502)
    new_session_id = extract_created_session_id(forked)
    if not new_session_id:
        return web.json_response(error_payload("opencode_fork_missing_session_id"), status=502)
    title = str(body.get("title") or binding.title or "Fork")
    new_binding = _store(request).create(binding.agent_id, new_session_id, title=title)
    return web.json_response(ok_payload(conversation=public_conversation(new_binding), fork={"parent_conversation_id": binding.portal_conversation_id}))


async def conversation_permission_handler(request: web.Request) -> web.Response:
    binding = _binding_for_request(request)
    if binding is None:
        return _conversation_not_found()
    body = await read_json_object(request)
    try:
        payload = map_permission_response(body)
    except ValueError as exc:
        return web.json_response(error_payload(str(exc)), status=400)
    permission_id = request.match_info["permission_id"]
    try:
        if hasattr(_client(request), "permission_response"):
            await _client(request).permission_response(binding.opencode_session_id, permission_id, payload)
        else:
            await _client(request).respond_permission(binding.opencode_session_id, permission_id, payload)
    except OpenCodeClientError as exc:
        return web.json_response(error_payload("opencode_error", detail=public_error_detail(exc)), status=502)
    return web.json_response(ok_payload())


async def opencode_mcp_handler(request: web.Request) -> web.Response:
    try:
        if hasattr(_client(request), "mcp_status"):
            payload = await _client(request).mcp_status()
        else:
            payload = await _client(request).mcp()
    except OpenCodeClientError as exc:
        return web.json_response(error_payload("opencode_error", detail=public_error_detail(exc)), status=502)
    servers = payload.get("servers") if isinstance(payload, dict) and isinstance(payload.get("servers"), dict) else {}
    if not servers and isinstance(payload, dict) and "success" not in payload and "tools" not in payload:
        servers = payload
    return web.json_response(ok_payload(servers=servers))


def register_opencode_thin_routes(app: web.Application) -> None:
    app.router.add_get("/api/opencode/health", opencode_health_handler)
    app.router.add_get("/api/opencode/mcp", opencode_mcp_handler)
    app.router.add_post("/api/opencode/conversations", create_conversation_handler)
    app.router.add_get("/api/opencode/conversations", list_conversations_handler)
    app.router.add_get("/api/opencode/conversations/{conversation_id}", get_conversation_handler)
    app.router.add_patch("/api/opencode/conversations/{conversation_id}", patch_conversation_handler)
    app.router.add_delete("/api/opencode/conversations/{conversation_id}", delete_conversation_handler)
    app.router.add_get("/api/opencode/conversations/{conversation_id}/status", conversation_status_handler)
    app.router.add_get("/api/opencode/conversations/{conversation_id}/messages", conversation_messages_handler)
    app.router.add_post("/api/opencode/conversations/{conversation_id}/send", send_conversation_handler)
    app.router.add_post("/api/opencode/conversations/{conversation_id}/abort", abort_conversation_handler)
    app.router.add_get("/api/opencode/conversations/{conversation_id}/events", conversation_events_handler)
    app.router.add_get("/api/opencode/conversations/{conversation_id}/children", conversation_children_handler)
    app.router.add_get("/api/opencode/conversations/{conversation_id}/todo", conversation_todo_handler)
    app.router.add_get("/api/opencode/conversations/{conversation_id}/diff", conversation_diff_handler)
    app.router.add_post("/api/opencode/conversations/{conversation_id}/fork", conversation_fork_handler)
    app.router.add_post("/api/opencode/conversations/{conversation_id}/permissions/{permission_id}", conversation_permission_handler)
