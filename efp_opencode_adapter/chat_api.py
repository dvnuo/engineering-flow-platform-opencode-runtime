from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Iterable
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
    USER_DISPLAY_STORE_KEY,
)
from .attachment_service import build_opencode_attachment_parts
from .opencode_client import OpenCodeClientError
from .opencode_config import normalize_opencode_provider_id
from .opencode_ids import is_opencode_message_id, new_opencode_message_id, require_opencode_message_id
from .opencode_message_adapter import (
    extract_assistant_message_ids,
    extract_last_assistant_visible_text,
    extract_reasoning_texts_from_parts,
    message_id as adapter_message_id,
    message_role as adapter_message_role,
)
from .repository_workspace import ensure_repo_checkout, parse_create_pr_repo_request
from .session_store import SessionDeletedError, SessionRecord
from .skill_invocation import build_skill_prompt, evaluate_skill_invocation, parse_slash_invocation
from .thinking_events import safe_preview, utc_now_iso
from .trace_context import add_trace_context, build_trace_context, profile_version_from_metadata


logger = logging.getLogger(__name__)

FINAL_RESPONSE_CONTRACT_SUFFIX = (
    "\n\nRuntime contract: Return the final visible answer for this request. "
    "If blocked, state the exact blocker."
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
        for key in ("id", "session_id", "sessionID", "uuid"):
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


def _requested_message_id(payload: dict[str, Any]) -> str:
    value = payload.get("message_id") if payload.get("message_id") is not None else payload.get("opencode_message_id")
    if value is None or value == "":
        return new_opencode_message_id()
    try:
        return require_opencode_message_id(value, field="message_id")
    except ValueError as exc:
        raise _bad_request(str(exc))


def _detect_new_message_ids(before_messages: list[dict[str, Any]], after_messages: list[dict[str, Any]]) -> tuple[str, str]:
    before_ids = {adapter_message_id(message) for message in before_messages if adapter_message_id(message)}
    user_message_id = ""
    assistant_message_id = ""
    for message in after_messages:
        current_id = adapter_message_id(message)
        if not current_id or current_id in before_ids:
            continue
        role = adapter_message_role(message).lower()
        if role == "user":
            user_message_id = current_id
        elif role == "assistant":
            assistant_message_id = current_id
    return user_message_id, assistant_message_id


def _append_unique_message_ids(target: list[str], candidates: Iterable[str]) -> None:
    seen = set(target)
    for candidate in candidates:
        if candidate and candidate not in seen:
            target.append(candidate)
            seen.add(candidate)


async def _ensure_record_for_chat(*, client, store, portal_session_id: str, title: str, agent: str | None, model: str | None) -> tuple[SessionRecord, bool]:
    existing = store.get(portal_session_id)
    if existing is not None and existing.deleted:
        raise SessionDeletedError("session_deleted")
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


async def _send_message(
    client: Any,
    session_id: str,
    *,
    parts: list[dict[str, Any]],
    model: str | None,
    agent: str | None,
    system: str | None,
    message_id: str,
) -> Any:
    if hasattr(client, "message"):
        body: dict[str, Any] = {"parts": parts, "messageID": message_id}
        if model:
            body["model"] = model
        if agent:
            body["agent"] = agent
        if system:
            body["system"] = system
        return await client.message(session_id, body)
    return await client.send_message(session_id, parts=parts, model=model, agent=agent, system=system, message_id=message_id)


def _event_payload(
    event_type: str,
    *,
    session_id: str,
    request_id: str,
    opencode_session_id: str,
    state: str,
    summary: str,
    data: dict[str, Any] | None = None,
    trace_context: dict[str, str],
) -> dict[str, Any]:
    event = {
        "type": event_type,
        "event_type": event_type,
        "engine": "opencode",
        "session_id": session_id,
        "request_id": request_id,
        "opencode_session_id": opencode_session_id,
        "state": state,
        "summary": safe_preview(summary, 500),
        "data": safe_preview(data or {}, 1000),
        "created_at": utc_now_iso(),
        "ts": time.time(),
    }
    return add_trace_context(event, trace_context)


async def _publish_event(bus, runtime_events: list[dict[str, Any]], event: dict[str, Any]) -> None:
    runtime_events.append(event)
    await bus.publish(event)


def _context_state(message: str, state: str, response: str = "") -> dict[str, Any]:
    return {
        "objective": message[:300],
        "summary": (response or message)[:500],
        "current_state": state,
        "next_step": "",
        "constraints": [],
        "decisions": [],
        "open_loops": [],
        "budget": {"usage_percent": 0},
    }


async def _build_chat_parts(app: web.Application, *, portal_session_id: str, message: str, attachments: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    parts: list[dict[str, Any]] = [{"type": "text", "text": message}]
    attachment_debug: list[dict[str, Any]] = []
    if not attachments:
        return parts, attachment_debug
    try:
        attachment_parts, attachment_debug = build_opencode_attachment_parts(
            app.get(ATTACHMENT_SERVICE_KEY),
            portal_session_id,
            attachments,
            max_text_chars=30000,
            max_inline_bytes=10 * 1024 * 1024,
        )
        parts.extend(attachment_parts)
    except Exception as exc:
        safe_error = safe_preview(str(exc), 300)
        attachment_debug.append({"status": "error", "error": safe_error})
        parts.append(
            {
                "type": "text",
                "text": f"Attachment processing failed: {safe_error}",
                "synthetic": True,
                "metadata": {"efp_internal": "attachment_context"},
            }
        )
    return parts, attachment_debug


async def _maybe_apply_skill_invocation(
    *,
    app: web.Application,
    client: Any,
    record: SessionRecord,
    message: str,
    parts: list[dict[str, Any]],
    model: str | None,
    agent: str | None,
    system: str | None,
    message_id: str,
    runtime_events: list[dict[str, Any]],
    trace_context: dict[str, str],
    portal_session_id: str,
    request_id: str,
) -> tuple[bool, Any, str | None, dict[str, Any] | None]:
    invocation = parse_slash_invocation(message)
    if invocation is None:
        return False, None, None, None

    bus = app[EVENT_BUS_KEY]
    settings = app[SETTINGS_KEY]
    skill_decision = evaluate_skill_invocation(settings, invocation)
    skill_debug: dict[str, Any] = {
        "kind": "skill",
        "raw_name": invocation.raw_name,
        "skill_name": invocation.skill_name,
        "arguments": invocation.arguments,
        "reason": skill_decision.reason,
        "permission_state": skill_decision.permission_state,
        "used_command_api": False,
        "used_skill_prompt": False,
        "blocked": False,
    }
    await _publish_event(
        bus,
        runtime_events,
        _event_payload(
            "skill.detected",
            session_id=portal_session_id,
            request_id=request_id,
            opencode_session_id=record.opencode_session_id,
            state="running",
            summary="Skill invocation detected.",
            data={"skill_name": invocation.skill_name, "raw_name": invocation.raw_name},
            trace_context=trace_context,
        ),
    )

    if skill_decision.reason == "unknown_skill" and hasattr(client, "list_commands"):
        try:
            available_commands = await client.list_commands()
            command_names = {str(c.get("name") or "") for c in available_commands if isinstance(c, dict)}
        except Exception as exc:
            command_names = set()
            skill_debug["command_lookup_error"] = safe_preview(str(exc), 300)
        if invocation.skill_name in command_names and not parts[1:]:
            response_payload = await client.execute_command(
                record.opencode_session_id,
                command=invocation.skill_name,
                arguments=invocation.arguments,
                model=model,
                agent=agent or "efp-main",
                message_id=message_id,
            )
            skill_debug.update({"kind": "command", "used_command_api": True, "native_command": True, "reason": "allowed"})
            return True, response_payload, None, skill_debug

    repository_context = None
    repo_request = None
    if skill_decision.allowed and invocation.skill_name == "create-pull-request":
        repo_request = parse_create_pr_repo_request(invocation.arguments)
        if repo_request is not None:
            checkout_result = ensure_repo_checkout(settings, repo_request)
            skill_debug["repository_preflight"] = {
                "attempted": True,
                "success": checkout_result.success,
                "path": checkout_result.path,
                "owner": checkout_result.owner,
                "repo": checkout_result.repo,
                "head_branch": checkout_result.head_branch,
                "base_branch": checkout_result.base_branch,
                "error": checkout_result.error,
            }
            if checkout_result.success:
                repository_context = checkout_result
            else:
                skill_decision = type(skill_decision)(
                    skill=skill_decision.skill,
                    allowed=False,
                    reason="repository_checkout_failed",
                    permission_state=skill_decision.permission_state,
                )
                skill_debug["reason"] = "repository_checkout_failed"

    if not skill_decision.allowed:
        if skill_decision.reason == "missing_required_writeback_tools":
            assistant_text = "Skill create-pull-request cannot run because required writeback tool github_create_pull_request / efp_github_create_pull_request is unavailable."
        elif skill_decision.reason == "repository_checkout_failed" and repo_request is not None:
            preflight = skill_debug.get("repository_preflight", {})
            assistant_text = (
                f"Repository checkout failed for {repo_request.repo_url} to {preflight.get('path', '')} "
                f"(head: {repo_request.head_branch}, base: {repo_request.base_branch}, failure: {preflight.get('error')})."
            )
        else:
            assistant_text = f"Skill `{invocation.skill_name}` cannot run in OpenCode runtime: {skill_decision.reason}."
        skill_debug["blocked"] = True
        await _publish_event(
            bus,
            runtime_events,
            _event_payload(
                "skill.blocked",
                session_id=portal_session_id,
                request_id=request_id,
                opencode_session_id=record.opencode_session_id,
                state="failed",
                summary=assistant_text,
                data={"skill_name": invocation.skill_name, "reason": skill_decision.reason},
                trace_context=trace_context,
            ),
        )
        return True, None, assistant_text, skill_debug

    prompt = build_skill_prompt(skill_decision.skill or {}, invocation)
    if repository_context is not None:
        prompt += (
            f"\n\nRepository has been prepared at: {repository_context.path}\n"
            "All local git inspection commands must run from that directory\n"
            f"Use base branch: {repository_context.base_branch}\n"
            f"Use head branch: {repository_context.head_branch}\n"
            "Do not run git inspection from /workspace unless /workspace/.git exists"
        )
    parts[0] = {
        **parts[0],
        "type": "text",
        "text": prompt,
        "synthetic": True,
        "metadata": {"efp_internal": "skill_prompt", "portal_request_id": request_id, "original_user_message_hidden": True},
    }
    skill_debug["used_skill_prompt"] = True
    await _publish_event(
        bus,
        runtime_events,
        _event_payload(
            "skill.prompt_applied",
            session_id=portal_session_id,
            request_id=request_id,
            opencode_session_id=record.opencode_session_id,
            state="running",
            summary="Skill prompt sent to OpenCode.",
            data={"skill_name": invocation.skill_name},
            trace_context=trace_context,
        ),
    )
    response_payload = await _send_message(
        client,
        record.opencode_session_id,
        parts=parts,
        model=model,
        agent=agent,
        system=system,
        message_id=message_id,
    )
    return True, response_payload, None, skill_debug


async def handle_chat_payload_for_app(app: web.Application, payload: dict[str, Any]) -> dict[str, Any]:
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        raise _bad_request("message_required")
    message = message.strip()

    metadata = _metadata_from_payload(payload)
    runtime_profile = _runtime_profile_from_metadata(metadata)
    portal_session_id = _portal_session_id_from_payload(payload)
    request_id = _request_id_from_payload(payload)
    title = _normalize_title(_optional_str(metadata.get("title")) or message[:60])
    model = _model_from_chat_payload(payload, metadata, runtime_profile)
    agent = _optional_str(payload.get("agent")) or _optional_str(metadata.get("agent"))
    system = _optional_str(payload.get("system")) or _optional_str(metadata.get("system"))
    system = f"{system}{FINAL_RESPONSE_CONTRACT_SUFFIX}" if system else FINAL_RESPONSE_CONTRACT_SUFFIX.strip()
    attachments = payload.get("attachments")
    initial_user_message_id = _requested_message_id(payload)

    store = app[SESSION_STORE_KEY]
    bus = app[EVENT_BUS_KEY]
    client = app[OPENCODE_CLIENT_KEY]
    chatlog_store = app[CHATLOG_STORE_KEY]
    usage_tracker = app[USAGE_TRACKER_KEY]
    portal_metadata_client = app[PORTAL_METADATA_CLIENT_KEY]
    settings = app[SETTINGS_KEY]
    runtime_events: list[dict[str, Any]] = []
    opencode_session_id = ""
    provider_for_trace_raw = _optional_str(runtime_profile.get("provider")) or _optional_str(metadata.get("provider"))
    provider_for_trace = normalize_opencode_provider_id(provider_for_trace_raw)
    profile_version, runtime_profile_id = profile_version_from_metadata(metadata, runtime_profile)
    trace_context: dict[str, str] = {}
    context_state = _context_state(message, "running")

    try:
        record, partial_recovery = await _ensure_record_for_chat(
            client=client,
            store=store,
            portal_session_id=portal_session_id,
            title=title,
            agent=agent,
            model=model,
        )
        opencode_session_id = record.opencode_session_id
        trace_context = build_trace_context(
            settings,
            request_id=request_id,
            session_id=portal_session_id,
            opencode_session_id=record.opencode_session_id,
            profile_version=profile_version,
            runtime_profile_id=runtime_profile_id,
            model=model or "",
            provider=provider_for_trace or "",
        )

        started = _event_payload(
            "chat.started",
            session_id=portal_session_id,
            request_id=request_id,
            opencode_session_id=record.opencode_session_id,
            state="running",
            summary="Chat started.",
            data={"session_id": portal_session_id, "request_id": request_id},
            trace_context=trace_context,
        )
        await _publish_event(bus, runtime_events, started)
        chatlog_store.start_entry(
            portal_session_id,
            request_id=request_id,
            message=message,
            runtime_events=runtime_events,
            context_state=context_state,
            llm_debug={"engine": "opencode", "opencode_session_id": record.opencode_session_id, "trace_context": trace_context},
        )
        await portal_metadata_client.publish_session_metadata(
            session_id=portal_session_id,
            latest_event_type="chat.started",
            latest_event_state="running",
            request_id=request_id,
            summary="Chat started",
            runtime_events=runtime_events,
            metadata={"opencode_session_id": record.opencode_session_id, "trace_context": trace_context},
        )
        thinking = _event_payload(
            "llm_thinking",
            session_id=portal_session_id,
            request_id=request_id,
            opencode_session_id=record.opencode_session_id,
            state="running",
            summary="OpenCode is thinking.",
            data={"message": "OpenCode is thinking"},
            trace_context=trace_context,
        )
        await _publish_event(bus, runtime_events, thinking)

        before_messages: list[dict[str, Any]] = []
        try:
            before_messages = await client.list_messages(record.opencode_session_id)
        except Exception:
            before_messages = []

        parts, attachment_debug = await _build_chat_parts(app, portal_session_id=portal_session_id, message=message, attachments=attachments)
        display_store = app.get(USER_DISPLAY_STORE_KEY)
        if display_store is not None:
            try:
                display_store.put_user_message(
                    portal_session_id=portal_session_id,
                    opencode_session_id=record.opencode_session_id,
                    opencode_message_id=initial_user_message_id,
                    display_content=message,
                    display_attachments=display_store.sanitize_display_attachments(attachments),
                    metadata={
                        "source": "portal_original_user_message",
                        "request_id": request_id,
                        "internal_model_content_hidden": True,
                    },
                )
            except Exception:
                logger.warning("failed to save user display message", exc_info=True)

        skill_handled, response_payload, synthetic_response, skill_debug = await _maybe_apply_skill_invocation(
            app=app,
            client=client,
            record=record,
            message=message,
            parts=parts,
            model=model,
            agent=agent,
            system=system,
            message_id=initial_user_message_id,
            runtime_events=runtime_events,
            trace_context=trace_context,
            portal_session_id=portal_session_id,
            request_id=request_id,
        )
        if not skill_handled:
            response_payload = await _send_message(
                client,
                record.opencode_session_id,
                parts=parts,
                model=model,
                agent=agent,
                system=system,
                message_id=initial_user_message_id,
            )

        after_messages = await client.list_messages(record.opencode_session_id)
        response_text = synthetic_response or extract_assistant_text(response_payload) or extract_assistant_text(after_messages)
        if response_text.strip():
            assistant_text = response_text.strip()
            completion_state = "completed"
            incomplete_reason = ""
            ok = synthetic_response is None
        else:
            assistant_text = "OpenCode completed without a visible assistant response."
            completion_state = "empty_final"
            incomplete_reason = "empty_final_assistant_text"
            ok = False

        payload_message = response_payload.get("message") if isinstance(response_payload, dict) else None
        if isinstance(payload_message, dict):
            for item in extract_reasoning_texts_from_parts(payload_message.get("parts")):
                reasoning = _event_payload(
                    "llm_thinking",
                    session_id=portal_session_id,
                    request_id=request_id,
                    opencode_session_id=record.opencode_session_id,
                    state="running",
                    summary=safe_preview(item, 300),
                    data={"message": safe_preview(item, 300)},
                    trace_context=trace_context,
                )
                await _publish_event(bus, runtime_events, reasoning)

        user_message_id, assistant_message_id = _detect_new_message_ids(before_messages, after_messages)
        user_message_id = user_message_id or initial_user_message_id
        assistant_message_ids: list[str] = []
        before_assistant_ids = set(extract_assistant_message_ids(before_messages))
        _append_unique_message_ids(assistant_message_ids, extract_assistant_message_ids(after_messages, exclude_message_ids=before_assistant_ids))
        _append_unique_message_ids(assistant_message_ids, extract_assistant_message_ids(response_payload, exclude_message_ids=before_assistant_ids))
        if not assistant_message_id and isinstance(response_payload, dict):
            candidate = response_payload.get("info", {}).get("id") if isinstance(response_payload.get("info"), dict) else ""
            if not candidate and isinstance(response_payload.get("message"), dict):
                candidate = adapter_message_id(response_payload["message"])
            assistant_message_id = str(candidate or "")
        _append_unique_message_ids(assistant_message_ids, [assistant_message_id])
        if assistant_message_ids:
            assistant_message_id = assistant_message_ids[-1]

        terminal_type = "chat.completed" if completion_state == "completed" else "chat.failed"
        terminal_state = "success" if completion_state == "completed" else completion_state
        terminal = _event_payload(
            terminal_type,
            session_id=portal_session_id,
            request_id=request_id,
            opencode_session_id=record.opencode_session_id,
            state=terminal_state,
            summary=assistant_text,
            data={"message": assistant_text, "completion_state": completion_state},
            trace_context=trace_context,
        )
        await _publish_event(bus, runtime_events, terminal)

        updated = store.update_after_chat(portal_session_id, message, assistant_text, model, agent)
        usage_record = usage_tracker.record_chat(
            session_id=portal_session_id,
            request_id=request_id,
            model=model,
            provider=provider_for_trace,
            response_payload=response_payload,
            input_text=message,
            output_text=assistant_text,
        )
        usage_record["request_id"] = trace_context.get("request_id", usage_record.get("request_id", ""))
        final_context = _context_state(message, "completed" if completion_state == "completed" else completion_state, assistant_text)
        llm_debug: dict[str, Any] = {
            "engine": "opencode",
            "opencode_session_id": updated.opencode_session_id,
            "usage": usage_record,
            "response_payload_preview": safe_preview(_redact_attachment_payloads_for_debug(response_payload), 2000),
            "trace_context": trace_context,
            "message_ids": {
                "user_message_id": user_message_id,
                "assistant_message_id": assistant_message_id,
                "assistant_message_ids": assistant_message_ids,
            },
            "attachments": attachment_debug,
        }
        if skill_debug:
            llm_debug["skill_invocation"] = skill_debug
        if partial_recovery or getattr(updated, "partial_recovery", False):
            llm_debug["partial_recovery"] = True
        chatlog_store.finish_entry(
            portal_session_id,
            request_id=request_id,
            status="success" if completion_state == "completed" else completion_state,
            response=assistant_text,
            runtime_events=runtime_events,
            events=runtime_events,
            context_state=final_context,
            llm_debug=llm_debug,
        )
        await portal_metadata_client.publish_session_metadata(
            session_id=portal_session_id,
            latest_event_type=terminal_type,
            latest_event_state=terminal_state,
            request_id=request_id,
            summary=assistant_text[:300],
            runtime_events=runtime_events,
            metadata={
                "engine": "opencode",
                "opencode_session_id": updated.opencode_session_id,
                "model": usage_record.get("model") or model or "unknown",
                "provider": usage_record.get("provider") or provider_for_trace or "unknown",
                "context_state": final_context,
                "usage": usage_record,
                "trace_context": trace_context,
            },
        )
        return {
            "ok": ok,
            "completion_state": completion_state,
            "incomplete_reason": incomplete_reason,
            "session_id": portal_session_id,
            "request_id": trace_context.get("request_id", request_id),
            "response": assistant_text,
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
            "assistant_message_ids": assistant_message_ids,
            "events": runtime_events,
            "runtime_events": runtime_events,
            "usage": usage_record,
            "metadata": {},
            "context_state": final_context,
            "_llm_debug": llm_debug,
        }
    except SessionDeletedError:
        raise web.HTTPGone(text=json.dumps({"error": "session_deleted", "session_id": portal_session_id, "detail": "Session was deleted"}), content_type="application/json")
    except OpenCodeClientError as exc:
        if not trace_context:
            trace_context = build_trace_context(
                settings,
                request_id=request_id,
                session_id=portal_session_id,
                opencode_session_id=opencode_session_id,
                profile_version=profile_version,
                runtime_profile_id=runtime_profile_id,
                model=model or "",
                provider=provider_for_trace or "",
            )
        failed = _event_payload(
            "chat.failed",
            session_id=portal_session_id,
            request_id=request_id,
            opencode_session_id=opencode_session_id,
            state="failed",
            summary=str(exc),
            data={"error": str(exc)},
            trace_context=trace_context,
        )
        await _publish_event(bus, runtime_events, failed)
        chatlog_store.fail_entry(
            portal_session_id,
            request_id=request_id,
            error=str(exc),
            runtime_events=runtime_events,
            context_state=_context_state(message, "failed", str(exc)),
            llm_debug={"engine": "opencode", "opencode_session_id": opencode_session_id, "trace_context": trace_context},
        )
        await portal_metadata_client.publish_session_metadata(
            session_id=portal_session_id,
            latest_event_type="chat.failed",
            latest_event_state="failed",
            request_id=request_id,
            summary=str(exc),
            runtime_events=runtime_events,
            metadata={"engine": "opencode", "trace_context": trace_context},
        )
        raise web.HTTPBadGateway(text=json.dumps({"error": "opencode_error", "detail": str(exc)}), content_type="application/json")
    except web.HTTPException:
        raise
    except Exception as exc:
        detail = safe_preview(str(exc) or exc.__class__.__name__, 1000)
        if not trace_context:
            trace_context = build_trace_context(settings, request_id=request_id, session_id=portal_session_id, opencode_session_id=opencode_session_id)
        failed = _event_payload(
            "chat.failed",
            session_id=portal_session_id,
            request_id=request_id,
            opencode_session_id=opencode_session_id,
            state="failed",
            summary=str(detail),
            data={"error": detail},
            trace_context=trace_context,
        )
        await _publish_event(bus, runtime_events, failed)
        chatlog_store.fail_entry(
            portal_session_id,
            request_id=request_id,
            error=str(detail),
            runtime_events=runtime_events,
            context_state=_context_state(message, "failed", str(detail)),
            llm_debug={"engine": "opencode", "opencode_session_id": opencode_session_id, "trace_context": trace_context},
        )
        raise web.HTTPInternalServerError(text=json.dumps({"error": "chat_failed", "detail": detail}), content_type="application/json")


async def handle_chat_payload(request: web.Request, payload: dict[str, Any]) -> dict[str, Any]:
    return await handle_chat_payload_for_app(request.app, payload)


async def chat_handler(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:
        raise _bad_request("invalid_json")
    if not isinstance(payload, dict):
        raise _bad_request("invalid_json")
    result = await handle_chat_payload_for_app(request.app, payload)
    return web.json_response(result)


class SSEClientDisconnected(ConnectionError):
    pass


async def _write_sse(resp: web.StreamResponse, event: str, data: dict[str, Any]) -> None:
    try:
        await resp.write(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8"))
    except (ConnectionResetError, RuntimeError) as exc:
        raise SSEClientDisconnected(str(exc)) from exc


async def _safe_write_eof(resp: web.StreamResponse) -> None:
    try:
        await resp.write_eof()
    except (ConnectionResetError, RuntimeError):
        pass


def _stream_error_final_payload(*, error_payload: dict[str, Any], session_id: str, request_id: str, runtime_events: list[dict[str, Any]] | None = None, settings: Any = None) -> dict[str, Any]:
    return {
        "ok": False,
        "completion_state": "error",
        "incomplete_reason": str(error_payload.get("error") or "chat_failed"),
        "session_id": session_id,
        "request_id": request_id,
        "response": str(error_payload.get("detail") or error_payload.get("error") or "chat_failed"),
        "events": runtime_events or [],
        "runtime_events": runtime_events or [],
        "usage": {},
        "metadata": {},
        "context_state": _context_state(str(error_payload.get("detail") or ""), "failed", str(error_payload.get("error") or "chat_failed")),
        "_llm_debug": {"engine": "opencode"},
    }


async def _stream_error_response(request: web.Request, error: str, detail: str | None = None, *, session_id: str = "", request_id: str = "") -> web.StreamResponse:
    resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache", "Connection": "close"})
    await resp.prepare(request)
    error_payload = {"error": error, "detail": detail or error, "session_id": session_id, "request_id": request_id}
    final_payload = _stream_error_final_payload(error_payload=error_payload, session_id=session_id, request_id=request_id, runtime_events=[], settings=request.app.get(SETTINGS_KEY))
    try:
        await _write_sse(resp, "error", error_payload)
        await _write_sse(resp, "final", final_payload)
        await _write_sse(resp, "done", {"ok": True})
    except SSEClientDisconnected:
        pass
    finally:
        await _safe_write_eof(resp)
    return resp


def _http_error_payload(exc: web.HTTPException, *, session_id: str, request_id: str) -> dict[str, Any]:
    detail = exc.text or exc.reason or "chat_failed"
    error = "chat_failed"
    try:
        parsed = json.loads(detail)
        if isinstance(parsed, dict):
            error = str(parsed.get("error") or error)
            detail = str(parsed.get("detail") or parsed.get("error") or detail)
    except Exception:
        pass
    return {"error": error, "detail": detail, "session_id": session_id, "request_id": request_id}


async def chat_stream_handler(request: web.Request) -> web.StreamResponse:
    try:
        payload = await request.json()
    except Exception:
        return await _stream_error_response(request, "invalid_json")
    if not isinstance(payload, dict):
        return await _stream_error_response(request, "invalid_json")

    try:
        session_id = _portal_session_id_from_payload(payload)
        request_id = _request_id_from_payload(payload)
    except web.HTTPException as exc:
        return await _stream_error_response(request, "chat_failed", exc.text, session_id=str(payload.get("session_id") or ""), request_id=str(payload.get("request_id") or ""))

    payload = {**payload, "session_id": session_id, "request_id": request_id}
    resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache", "Connection": "close"})
    await resp.prepare(request)
    try:
        await _write_sse(resp, "chat.started", {"session_id": session_id, "request_id": request_id, "chatlog_id": request_id, "completion_state": "running"})
        final_payload = await handle_chat_payload_for_app(request.app, payload)
        await _write_sse(resp, "final", final_payload)
        await _write_sse(resp, "done", {"ok": True})
    except SSEClientDisconnected:
        return resp
    except web.HTTPException as exc:
        error_payload = _http_error_payload(exc, session_id=session_id, request_id=request_id)
        final_payload = _stream_error_final_payload(error_payload=error_payload, session_id=session_id, request_id=request_id, runtime_events=[], settings=request.app.get(SETTINGS_KEY))
        try:
            await _write_sse(resp, "error", error_payload)
            await _write_sse(resp, "final", final_payload)
            await _write_sse(resp, "done", {"ok": True})
        except SSEClientDisconnected:
            return resp
    except Exception as exc:
        error_payload = {"error": "chat_failed", "detail": safe_preview(str(exc), 500), "session_id": session_id, "request_id": request_id}
        final_payload = _stream_error_final_payload(error_payload=error_payload, session_id=session_id, request_id=request_id, runtime_events=[], settings=request.app.get(SETTINGS_KEY))
        try:
            await _write_sse(resp, "error", error_payload)
            await _write_sse(resp, "final", final_payload)
            await _write_sse(resp, "done", {"ok": True})
        except SSEClientDisconnected:
            return resp
    await _safe_write_eof(resp)
    return resp
