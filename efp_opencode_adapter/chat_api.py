from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import inspect
import os
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
    REQUEST_BINDING_STORE_KEY,
    SETTINGS_KEY,
    SESSION_STORE_KEY,
    USAGE_TRACKER_KEY,
    USER_DISPLAY_STORE_KEY,
)
from .attachment_service import build_opencode_attachment_parts
from .chat_run_registry import chat_run_registry
from .opencode_client import OpenCodeClientError
from .opencode_config import normalize_opencode_provider_id
from .opencode_ids import is_opencode_message_id, new_opencode_message_id, require_opencode_message_id
from .opencode_message_adapter import (
    extract_assistant_message_ids,
    extract_last_assistant_visible_text,
    extract_reasoning_texts_from_parts,
    find_latest_assistant_completion,
    message_id as adapter_message_id,
    message_role as adapter_message_role,
    message_to_visible_text,
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
TERMINAL_ASSISTANT_COMPLETION_STATES = {"completed", "blocked", "error", "empty_final"}
RUNNING_CHATLOG_STATUSES = {"running", "accepted", "queued", "in_progress"}
RECOVERABLE_SEND_ACCEPTANCE_PROBE_SECONDS = 5.0


def _stable_runtime_event_id(*, event_type: str, session_id: str, request_id: str, opencode_session_id: str, data: dict[str, Any] | None) -> str:
    try:
        payload = json.dumps(safe_preview(data or {}, 1000), sort_keys=True, default=str, ensure_ascii=False)
    except Exception:
        payload = str(safe_preview(data or {}, 1000))
    seed = f"{event_type}\n{session_id}\n{request_id}\n{opencode_session_id}\n{payload}"
    digest = hashlib.sha256(seed.encode("utf-8", errors="ignore")).hexdigest()[:16]
    prefix = f"opencode:{request_id or session_id or opencode_session_id}:{event_type}"
    if len(prefix) > 120:
        prefix = f"opencode:{digest}:{event_type}"
    return f"{prefix}:{digest}"


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
    kwargs: dict[str, Any] = {"parts": parts, "model": model, "agent": agent, "system": system}
    if hasattr(client, "send_message") and callable(getattr(client, "send_message")):
        try:
            sig = inspect.signature(client.send_message)
            accepts_message_id = "message_id" in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        except (TypeError, ValueError):
            accepts_message_id = True
        if accepts_message_id:
            kwargs["message_id"] = message_id
        return await client.send_message(session_id, **kwargs)

    if hasattr(client, "prompt_async") and callable(getattr(client, "prompt_async")):
        payload: dict[str, Any] = {"parts": parts, "messageID": message_id}
        if model:
            payload["model"] = model
        if agent:
            payload["agent"] = agent
        if system:
            payload["system"] = system
        return await client.prompt_async(session_id, payload)

    raise OpenCodeClientError("OpenCode client does not support send_message")


def _message_recovery_key(message: Any) -> str:
    mid = adapter_message_id(message)
    if mid:
        return f"id:{mid}"
    role = adapter_message_role(message).lower()
    text = message_to_visible_text(message).strip()
    if role or text:
        return f"{role}:{text}"
    return ""


def _is_recoverable_send_disconnect(exc: OpenCodeClientError, opencode_session_id: str) -> bool:
    if not getattr(exc, "is_recoverable_transport_error", False):
        return False
    method = str(getattr(exc, "method", "") or "").upper()
    if method and method != "POST":
        return False
    path = str(getattr(exc, "path", "") or "")
    if path:
        normalized_path = path.split("?", 1)[0].rstrip("/")
        expected_path = f"/session/{opencode_session_id}/message"
        if normalized_path != expected_path:
            return False
    return True


def _detect_recovered_send_acceptance(
    *,
    before_messages: list[dict[str, Any]],
    after_messages: list[dict[str, Any]],
    user_message_id: str,
    expected_user_text: str,
) -> str:
    before_keys = {key for message in before_messages if (key := _message_recovery_key(message))}
    expected_text = expected_user_text.strip()
    for message in after_messages:
        mid = adapter_message_id(message)
        if user_message_id and mid == user_message_id:
            return "user_message_id"
        key = _message_recovery_key(message)
        if key and key in before_keys:
            continue
        role = adapter_message_role(message).lower()
        if role == "user" and expected_text and message_to_visible_text(message).strip() == expected_text:
            return "matching_user_text"
    return ""


async def _probe_recoverable_send_acceptance(
    *,
    client: Any,
    opencode_session_id: str,
    before_messages: list[dict[str, Any]],
    user_message_id: str,
    expected_user_text: str,
    timeout_seconds: float,
    poll_seconds: float,
) -> dict[str, Any]:
    timeout = max(0.0, min(RECOVERABLE_SEND_ACCEPTANCE_PROBE_SECONDS, timeout_seconds))
    poll_interval = max(0.05, min(max(0.05, poll_seconds), 0.5))
    deadline = time.monotonic() + timeout
    attempts = 0
    last_messages: list[dict[str, Any]] = []
    last_error = ""
    while True:
        attempts += 1
        try:
            last_messages = await client.list_messages(opencode_session_id)
            last_error = ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = str(safe_preview(str(exc) or exc.__class__.__name__, 500))
            last_messages = []
        else:
            detected_by = _detect_recovered_send_acceptance(
                before_messages=before_messages,
                after_messages=last_messages,
                user_message_id=user_message_id,
                expected_user_text=expected_user_text,
            )
            if detected_by:
                return {
                    "accepted": True,
                    "detected_by": detected_by,
                    "attempts": attempts,
                    "messages": last_messages,
                }

        now = time.monotonic()
        if now >= deadline:
            return {
                "accepted": False,
                "detected_by": "",
                "attempts": attempts,
                "messages": last_messages,
                "probe_error": last_error,
                "timeout_seconds": timeout,
            }
        await asyncio.sleep(min(poll_interval, max(0.0, deadline - now)))


async def _send_message_with_recoverable_transport_probe(
    *,
    client: Any,
    opencode_session_id: str,
    parts: list[dict[str, Any]],
    model: str | None,
    agent: str | None,
    system: str | None,
    message_id: str,
    before_messages: list[dict[str, Any]],
    expected_user_text: str,
    settings: Any,
) -> tuple[Any, dict[str, Any] | None]:
    try:
        return (
            await _send_message(
                client,
                opencode_session_id,
                parts=parts,
                model=model,
                agent=agent,
                system=system,
                message_id=message_id,
            ),
            None,
        )
    except OpenCodeClientError as exc:
        if not _is_recoverable_send_disconnect(exc, opencode_session_id):
            raise
        recovery = await _probe_recoverable_send_acceptance(
            client=client,
            opencode_session_id=opencode_session_id,
            before_messages=before_messages,
            user_message_id=message_id,
            expected_user_text=expected_user_text,
            timeout_seconds=float(getattr(settings, "chat_completion_timeout_seconds", RECOVERABLE_SEND_ACCEPTANCE_PROBE_SECONDS)),
            poll_seconds=float(getattr(settings, "chat_completion_poll_seconds", 1.0)),
        )
        debug = {
            "accepted": bool(recovery.get("accepted")),
            "detected_by": str(recovery.get("detected_by") or ""),
            "attempts": int(recovery.get("attempts") or 0),
            "original_error": safe_preview(str(exc), 500),
        }
        if recovery.get("probe_error"):
            debug["probe_error"] = recovery.get("probe_error")
        if not recovery.get("accepted"):
            raise
        return {"messages": recovery.get("messages") or []}, debug


def _is_terminal_assistant_completion(probe: dict[str, Any]) -> bool:
    return str(probe.get("completion_state") or "") in TERMINAL_ASSISTANT_COMPLETION_STATES


def _assistant_completion_timeout_probe(
    *,
    last_probe: dict[str, Any],
    last_assistant_id: str,
    timeout_seconds: float,
    poll_seconds: float,
    poll_attempts: int,
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {}
    probe_diagnostics = last_probe.get("diagnostics") if isinstance(last_probe, dict) else None
    if isinstance(probe_diagnostics, dict):
        diagnostics.update(probe_diagnostics)
    elif probe_diagnostics is not None:
        diagnostics["last_diagnostics"] = safe_preview(probe_diagnostics, 1000)
    diagnostics.update(
        {
            "last_completion_state": str(last_probe.get("completion_state") or "incomplete") if isinstance(last_probe, dict) else "incomplete",
            "last_reason": str(last_probe.get("reason") or "no_terminal_assistant_message") if isinstance(last_probe, dict) else "no_terminal_assistant_message",
            "poll_attempts": poll_attempts,
            "timeout_seconds": timeout_seconds,
            "poll_seconds": poll_seconds,
        }
    )
    return {
        "text": "",
        "message_id": last_assistant_id,
        "completion_state": "incomplete",
        "reason": "final_assistant_message_timeout",
        "diagnostics": diagnostics,
    }


async def _wait_for_visible_assistant_response(
    *,
    client: Any,
    opencode_session_id: str,
    response_payload: Any,
    before_messages: list[dict[str, Any]],
    timeout_seconds: float,
    poll_seconds: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    before_assistant_ids = set(extract_assistant_message_ids(before_messages))
    last_messages: list[dict[str, Any]] = []
    last_probe = find_latest_assistant_completion(response_payload, exclude_message_ids=before_assistant_ids)
    assistant_ids = extract_assistant_message_ids(response_payload, exclude_message_ids=before_assistant_ids)
    last_assistant_id = assistant_ids[-1] if assistant_ids else str(last_probe.get("message_id") or "")
    if _is_terminal_assistant_completion(last_probe):
        return last_probe, last_messages

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    poll_interval = max(0.01, poll_seconds)
    poll_attempts = 0
    while True:
        poll_attempts += 1
        last_messages = await client.list_messages(opencode_session_id)
        last_probe = find_latest_assistant_completion(last_messages, exclude_message_ids=before_assistant_ids)
        assistant_ids = extract_assistant_message_ids(last_messages, exclude_message_ids=before_assistant_ids)
        if assistant_ids:
            last_assistant_id = assistant_ids[-1]
        elif last_probe.get("message_id"):
            last_assistant_id = str(last_probe.get("message_id") or "")

        if _is_terminal_assistant_completion(last_probe):
            return last_probe, last_messages

        now = time.monotonic()
        if now >= deadline:
            return (
                _assistant_completion_timeout_probe(
                    last_probe=last_probe,
                    last_assistant_id=last_assistant_id,
                    timeout_seconds=timeout_seconds,
                    poll_seconds=poll_seconds,
                    poll_attempts=poll_attempts,
                ),
                last_messages,
            )
        await asyncio.sleep(min(poll_interval, max(0.0, deadline - now)))


def _non_success_assistant_text(completion_state: str, reason: str) -> str:
    if completion_state == "empty_final":
        return "OpenCode completed without a visible assistant response."
    if reason == "final_assistant_message_timeout":
        return (
            "OpenCode is still working on this request in the background; the chat "
            "response timed out waiting for the final assistant message. The result "
            "will appear in this session's history once the run completes."
        )
    if completion_state == "blocked":
        return "OpenCode is blocked before producing a final visible assistant response."
    if completion_state == "error":
        return "OpenCode failed before producing a final visible assistant response."
    return "OpenCode did not produce a final visible assistant response."


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
    safe_data = safe_preview(data or {}, 1000)
    if not isinstance(safe_data, dict):
        safe_data = {}
    event = {
        "id": _stable_runtime_event_id(
            event_type=event_type,
            session_id=session_id,
            request_id=request_id,
            opencode_session_id=opencode_session_id,
            data=safe_data,
        ),
        "type": event_type,
        "event_type": event_type,
        "engine": "opencode",
        "session_id": session_id,
        "request_id": request_id,
        "opencode_session_id": opencode_session_id,
        "state": state,
        "summary": safe_preview(summary, 500),
        "data": safe_data,
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
    existing_record = store.get(portal_session_id)
    if existing_record is not None and not existing_record.deleted:
        opencode_session_id = existing_record.opencode_session_id
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
        bindings = app.get(REQUEST_BINDING_STORE_KEY)
        if bindings is not None and hasattr(bindings, "bind_active"):
            bindings.bind_active(record.opencode_session_id, portal_session_id, request_id, kind="chat")
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
        execution_started = _event_payload(
            "execution.started",
            session_id=portal_session_id,
            request_id=request_id,
            opencode_session_id=record.opencode_session_id,
            state="running",
            summary="OpenCode execution started.",
            data={"session_id": portal_session_id, "request_id": request_id},
            trace_context=trace_context,
        )
        await _publish_event(bus, runtime_events, execution_started)
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
        if bindings is not None and hasattr(bindings, "bind_message"):
            bindings.bind_message(record.opencode_session_id, initial_user_message_id, portal_session_id, request_id, kind="chat")
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

        send_recovery_debug: dict[str, Any] | None = None
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
            response_payload, send_recovery_debug = await _send_message_with_recoverable_transport_probe(
                client=client,
                opencode_session_id=record.opencode_session_id,
                parts=parts,
                model=model,
                agent=agent,
                system=system,
                message_id=initial_user_message_id,
                before_messages=before_messages,
                expected_user_text=message,
                settings=settings,
            )

        if synthetic_response is not None:
            after_messages = await client.list_messages(record.opencode_session_id)
            completion_probe = {
                "text": synthetic_response,
                "message_id": "",
                "completion_state": "completed",
                "reason": "synthetic_response",
                "diagnostics": {},
            }
        else:
            completion_probe, after_messages = await _wait_for_visible_assistant_response(
                client=client,
                opencode_session_id=record.opencode_session_id,
                response_payload=response_payload,
                before_messages=before_messages,
                timeout_seconds=settings.chat_completion_timeout_seconds,
                poll_seconds=settings.chat_completion_poll_seconds,
            )
            if not after_messages:
                try:
                    after_messages = await client.list_messages(record.opencode_session_id)
                except Exception:
                    after_messages = []

        completion_state = str(completion_probe.get("completion_state") or "incomplete")
        completion_reason = str(completion_probe.get("reason") or "")
        response_text = str(completion_probe.get("text") or "").strip()
        if completion_state == "completed" and response_text:
            assistant_text = response_text
            completion_state = "completed"
            incomplete_reason = ""
            ok = synthetic_response is None
        else:
            assistant_text = _non_success_assistant_text(completion_state, completion_reason)
            incomplete_reason = completion_reason or completion_state
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
        _append_unique_message_ids(assistant_message_ids, [str(completion_probe.get("message_id") or "")])
        if not assistant_message_id and isinstance(response_payload, dict):
            candidate = response_payload.get("info", {}).get("id") if isinstance(response_payload.get("info"), dict) else ""
            if not candidate and isinstance(response_payload.get("message"), dict):
                candidate = adapter_message_id(response_payload["message"])
            assistant_message_id = str(candidate or "")
        _append_unique_message_ids(assistant_message_ids, [assistant_message_id])
        if assistant_message_ids:
            assistant_message_id = assistant_message_ids[-1]
        if bindings is not None and hasattr(bindings, "bind_message"):
            for assistant_id in assistant_message_ids:
                bindings.bind_message(record.opencode_session_id, assistant_id, portal_session_id, request_id, kind="chat")

        terminal_type = "chat.completed" if completion_state == "completed" else "chat.failed"
        terminal_state = "success" if completion_state == "completed" else completion_state
        terminal_data = {"message": assistant_text, "completion_state": completion_state}
        if incomplete_reason:
            terminal_data["incomplete_reason"] = incomplete_reason
        if completion_state == "completed":
            assistant_delta = _event_payload(
                "assistant_delta",
                session_id=portal_session_id,
                request_id=request_id,
                opencode_session_id=record.opencode_session_id,
                state="running",
                summary=assistant_text,
                data={"delta": safe_preview(assistant_text, 500), "message": safe_preview(assistant_text, 500)},
                trace_context=trace_context,
            )
            await _publish_event(bus, runtime_events, assistant_delta)
            complete = _event_payload(
                "complete",
                session_id=portal_session_id,
                request_id=request_id,
                opencode_session_id=record.opencode_session_id,
                state="success",
                summary=assistant_text,
                data={"message": safe_preview(assistant_text, 500)},
                trace_context=trace_context,
            )
            await _publish_event(bus, runtime_events, complete)
        terminal = _event_payload(
            terminal_type,
            session_id=portal_session_id,
            request_id=request_id,
            opencode_session_id=record.opencode_session_id,
            state=terminal_state,
            summary=assistant_text,
            data=terminal_data,
            trace_context=trace_context,
        )
        execution_terminal = _event_payload(
            "execution.completed" if completion_state == "completed" else "execution.failed",
            session_id=portal_session_id,
            request_id=request_id,
            opencode_session_id=record.opencode_session_id,
            state=terminal_state,
            summary=assistant_text,
            data=terminal_data,
            trace_context=trace_context,
        )
        await _publish_event(bus, runtime_events, execution_terminal)
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
            "completion_probe": safe_preview(completion_probe, 2000),
            "attachments": attachment_debug,
        }
        if skill_debug:
            llm_debug["skill_invocation"] = skill_debug
        if send_recovery_debug:
            llm_debug["send_disconnect_probe"] = send_recovery_debug
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
        execution_failed = _event_payload(
            "execution.failed",
            session_id=portal_session_id,
            request_id=request_id,
            opencode_session_id=opencode_session_id,
            state="failed",
            summary=str(exc),
            data={"error": str(exc)},
            trace_context=trace_context,
        )
        await _publish_event(bus, runtime_events, execution_failed)
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
        execution_failed = _event_payload(
            "execution.failed",
            session_id=portal_session_id,
            request_id=request_id,
            opencode_session_id=opencode_session_id,
            state="failed",
            summary=str(detail),
            data={"error": detail},
            trace_context=trace_context,
        )
        await _publish_event(bus, runtime_events, execution_failed)
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
    raw_request_id = str(payload.get("request_id") or "").strip()
    if raw_request_id:
        # Idempotency guard: stream fallbacks may re-submit the same
        # request_id after a transport break while the original run is
        # still executing detached. Never execute the same request twice.
        raw_session_id = str(payload.get("session_id") or "").strip()
        existing_run = chat_run_registry.get(raw_request_id, session_id=raw_session_id or None)
        if existing_run is not None:
            if existing_run.terminal and isinstance(existing_run.final_payload, dict):
                return web.json_response(existing_run.final_payload)
            return web.json_response(
                {
                    "error": "duplicate_chat_request_id",
                    "message": "A chat run with this request_id already exists; reconnect to it instead of re-submitting.",
                    "session_id": existing_run.session_id,
                    "request_id": raw_request_id,
                    "state": existing_run.state,
                },
                status=409,
            )
    result = await handle_chat_payload_for_app(request.app, payload)
    return web.json_response(result)


class SSEClientDisconnected(ConnectionError):
    pass


async def _write_sse(resp: web.StreamResponse, event: str, data: dict[str, Any]) -> None:
    try:
        await resp.write(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8"))
    except (ConnectionResetError, RuntimeError) as exc:
        raise SSEClientDisconnected(str(exc)) from exc


def _chat_sse_keepalive_interval_seconds() -> float:
    """Idle interval between SSE keepalive comments on the chat stream.

    Long tool executions produce no runtime events; without periodic bytes,
    intermediaries with idle read timeouts (for example ingress-nginx's
    default 60s proxy-read-timeout) silently kill the stream mid-run.
    """
    raw = str(os.getenv("EFP_CHAT_SSE_KEEPALIVE_SECONDS", "")).strip()
    try:
        value = float(raw) if raw else 15.0
    except (TypeError, ValueError):
        return 15.0
    return max(1.0, value)


async def _write_sse_keepalive(resp: web.StreamResponse) -> None:
    """Write an SSE comment line; SSE parsers ignore lines starting with ':'."""
    try:
        await resp.write(b": keepalive\n\n")
    except (ConnectionResetError, RuntimeError) as exc:
        raise SSEClientDisconnected(str(exc)) from exc


async def _safe_write_eof(resp: web.StreamResponse) -> None:
    try:
        await resp.write_eof()
    except (ConnectionResetError, RuntimeError):
        pass


def _consume_background_chat_result(task: asyncio.Task) -> None:
    try:
        task.result()
    except Exception:
        logger.debug("detached chat stream task finished with error", exc_info=True)


async def _write_runtime_event_sse(resp: web.StreamResponse, event: dict[str, Any], seen_ids: set[str]) -> None:
    if not isinstance(event, dict):
        return
    event_id = str(event.get("id") or "")
    if event_id and event_id in seen_ids:
        return
    if event_id:
        seen_ids.add(event_id)
    event_data = event.get("data") if isinstance(event.get("data"), dict) else {}
    request_id = str(event.get("request_id") or event_data.get("request_id") or "")
    if request_id:
        chat_run_registry.record_event(request_id, event)
    await _write_sse(resp, "runtime_event", event)


async def _drain_runtime_event_queue(resp: web.StreamResponse, subscriber: Any, seen_ids: set[str]) -> None:
    while True:
        try:
            event = subscriber.queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        await _write_runtime_event_sse(resp, event, seen_ids)


async def _stream_runtime_events_until_done(resp: web.StreamResponse, subscriber: Any, chat_task: asyncio.Task, seen_ids: set[str]) -> None:
    keepalive_interval = _chat_sse_keepalive_interval_seconds()
    last_write_monotonic = time.monotonic()
    while not chat_task.done():
        try:
            event = await asyncio.wait_for(subscriber.queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
            if time.monotonic() - last_write_monotonic >= keepalive_interval:
                await _write_sse_keepalive(resp)
                last_write_monotonic = time.monotonic()
            continue
        await _write_runtime_event_sse(resp, event, seen_ids)
        last_write_monotonic = time.monotonic()
    await _drain_runtime_event_queue(resp, subscriber, seen_ids)


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


def _stream_failure_event(request: web.Request, *, session_id: str, request_id: str, error_payload: dict[str, Any]) -> dict[str, Any]:
    trace_context = build_trace_context(request.app[SETTINGS_KEY], request_id=request_id, session_id=session_id)
    return _event_payload(
        "execution.failed",
        session_id=session_id,
        request_id=request_id,
        opencode_session_id="",
        state="failed",
        summary=str(error_payload.get("detail") or error_payload.get("error") or "chat_failed"),
        data={"error": error_payload.get("error") or "chat_failed", "detail": error_payload.get("detail") or ""},
        trace_context=trace_context,
    )


async def _stream_error_response(request: web.Request, error: str, detail: str | None = None, *, session_id: str = "", request_id: str = "") -> web.StreamResponse:
    resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache", "Connection": "close"})
    await resp.prepare(request)
    error_payload = {"error": error, "detail": detail or error, "session_id": session_id, "request_id": request_id}
    runtime_event = _stream_failure_event(request, session_id=session_id, request_id=request_id, error_payload=error_payload)
    final_payload = _stream_error_final_payload(error_payload=error_payload, session_id=session_id, request_id=request_id, runtime_events=[runtime_event], settings=request.app.get(SETTINGS_KEY))
    try:
        await _write_sse(resp, "runtime_event", runtime_event)
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


def _chatlog_run_status(app: web.Application, *, session_id: str, request_id: str) -> dict[str, Any] | None:
    try:
        chatlog = app[CHATLOG_STORE_KEY].get(session_id)
    except Exception:
        chatlog = None
    if not isinstance(chatlog, dict):
        return None
    entries = chatlog.get("entries")
    if not isinstance(entries, list):
        return None
    for entry in reversed(entries):
        if not isinstance(entry, dict) or str(entry.get("request_id") or "") != request_id:
            continue
        status = str(entry.get("status") or "").strip().lower()
        if status in {"success", "completed", "complete"}:
            state = "completed"
        elif status in RUNNING_CHATLOG_STATUSES:
            state = "running"
        elif status in {"cancelled", "canceled"}:
            state = "cancelled"
        elif status:
            state = "failed"
        else:
            state = "unknown"
        final_payload = {
            "ok": state == "completed",
            "completion_state": "completed" if state == "completed" else state,
            "session_id": session_id,
            "request_id": request_id,
            "response": str(entry.get("response") or ""),
            "events": entry.get("events") if isinstance(entry.get("events"), list) else [],
            "runtime_events": entry.get("runtime_events") if isinstance(entry.get("runtime_events"), list) else [],
            "context_state": entry.get("context_state") if isinstance(entry.get("context_state"), dict) else None,
            "_llm_debug": entry.get("llm_debug") if isinstance(entry.get("llm_debug"), dict) else {},
        }
        return {
            "ok": True,
            "engine": "opencode",
            "session_id": session_id,
            "request_id": request_id,
            "state": state,
            "terminal": state in {"completed", "failed", "cancelled"},
            "started_at": str(entry.get("started_at") or ""),
            "updated_at": str(entry.get("updated_at") or chatlog.get("updated_at") or ""),
            "latest_event_at": str(entry.get("updated_at") or chatlog.get("updated_at") or ""),
            "latest_event_seq": 0,
            "replay_available": bool(final_payload["runtime_events"]),
            "final_payload": final_payload if state in {"completed", "failed", "cancelled"} else None,
            "source_of_truth": "chatlog",
        }
    return None


def _chat_run_status_payload(app: web.Application, *, session_id: str, request_id: str) -> dict[str, Any]:
    record = chat_run_registry.get(request_id, session_id=session_id or None)
    if record is not None:
        payload = record.to_payload()
        payload["source_of_truth"] = "run_registry"
        return payload
    fallback = _chatlog_run_status(app, session_id=session_id, request_id=request_id)
    if fallback is not None:
        return fallback
    return {
        "ok": False,
        "engine": "opencode",
        "session_id": session_id,
        "request_id": request_id,
        "state": "unknown",
        "terminal": False,
        "replay_available": False,
        "error": "chat_run_not_found",
    }


async def _stream_existing_chat_run(request: web.Request, *, session_id: str, request_id: str) -> web.StreamResponse:
    resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache", "Connection": "close"})
    await resp.prepare(request)
    try:
        await _write_sse(resp, "chat.started", {"session_id": session_id, "request_id": request_id, "completion_state": "running", "resumed": True})
        while True:
            payload = _chat_run_status_payload(request.app, session_id=session_id, request_id=request_id)
            await _write_sse(resp, "run_status", payload)
            if payload.get("terminal"):
                final_payload = payload.get("final_payload")
                if isinstance(final_payload, dict):
                    await _write_sse(resp, "final", final_payload)
                await _write_sse(resp, "done", {"ok": True, "session_id": session_id, "request_id": request_id})
                return resp
            if payload.get("state") == "unknown":
                await _write_sse(resp, "done", {"ok": False, "session_id": session_id, "request_id": request_id})
                return resp
            await asyncio.sleep(0.5)
    except SSEClientDisconnected:
        chat_run_registry.mark_detached(request_id)
        return resp


async def chat_run_status_handler(request: web.Request) -> web.Response:
    request_id = str(request.match_info.get("request_id") or "").strip()
    session_id = str(request.query.get("session_id") or "").strip()
    if not request_id:
        return web.json_response({"ok": False, "error": "request_id_required"}, status=400)
    payload = _chat_run_status_payload(request.app, session_id=session_id, request_id=request_id)
    status = 404 if payload.get("error") == "chat_run_not_found" else 200
    return web.json_response(payload, status=status)


async def chat_run_cancel_handler(request: web.Request) -> web.Response:
    request_id = str(request.match_info.get("request_id") or "").strip()
    session_id = str(request.query.get("session_id") or "").strip()
    if not request_id:
        return web.json_response({"ok": False, "error": "request_id_required"}, status=400)
    record = chat_run_registry.get(request_id, session_id=session_id or None)
    if record is None:
        return web.json_response({"ok": False, "error": "chat_run_not_found", "request_id": request_id, "session_id": session_id}, status=404)
    remote_cancel: dict[str, Any] = {}
    try:
        session_record = request.app[SESSION_STORE_KEY].get(record.session_id)
        opencode_session_id = str(getattr(session_record, "opencode_session_id", "") or "")
        if opencode_session_id:
            remote_cancel = await request.app[OPENCODE_CLIENT_KEY].cancel_message(opencode_session_id)
    except Exception as exc:
        remote_cancel = {"error": safe_preview(str(exc), 500)}
    cancelled = chat_run_registry.cancel(request_id)
    return web.json_response({
        "ok": cancelled,
        "engine": "opencode",
        "request_id": request_id,
        "session_id": record.session_id,
        "state": "cancelled" if cancelled else record.state,
        "terminal": True if cancelled else record.terminal,
        "remote_cancel": remote_cancel,
    })


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
    existing_run = chat_run_registry.get(request_id, session_id=session_id)
    if existing_run is not None or payload.get("reconnect") is True:
        return await _stream_existing_chat_run(request, session_id=session_id, request_id=request_id)
    resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache", "Connection": "close"})
    await resp.prepare(request)
    bus = request.app[EVENT_BUS_KEY]
    subscriber = bus.subscribe({"session_id": session_id, "request_id": request_id})
    chat_run_registry.start(session_id=session_id, request_id=request_id)
    chat_task = asyncio.create_task(handle_chat_payload_for_app(request.app, payload))
    chat_run_registry.attach_task(request_id, chat_task)

    def _record_chat_task_done(task: asyncio.Task) -> None:
        try:
            final = task.result()
            if isinstance(final, dict):
                chat_run_registry.complete(request_id, final)
        except asyncio.CancelledError:
            chat_run_registry.fail(request_id, {"error": "chat_cancelled", "session_id": session_id, "request_id": request_id})
        except Exception as exc:
            chat_run_registry.fail(request_id, {"error": "chat_failed", "detail": safe_preview(str(exc), 500), "session_id": session_id, "request_id": request_id})

    chat_task.add_done_callback(_record_chat_task_done)
    seen_event_ids: set[str] = set()
    try:
        await _write_sse(resp, "chat.started", {"session_id": session_id, "request_id": request_id, "chatlog_id": request_id, "completion_state": "running"})
        await _stream_runtime_events_until_done(resp, subscriber, chat_task, seen_event_ids)
        final_payload = await chat_task
        await _drain_runtime_event_queue(resp, subscriber, seen_event_ids)
        chat_run_registry.complete(request_id, final_payload)
        await _write_sse(resp, "final", final_payload)
        await _write_sse(resp, "done", {"ok": True})
    except SSEClientDisconnected:
        chat_run_registry.mark_detached(request_id)
        if not chat_task.done():
            chat_task.add_done_callback(_consume_background_chat_result)
        return resp
    except web.HTTPException as exc:
        if not chat_task.done():
            chat_task.cancel()
            await asyncio.gather(chat_task, return_exceptions=True)
        error_payload = _http_error_payload(exc, session_id=session_id, request_id=request_id)
        runtime_event = _stream_failure_event(request, session_id=session_id, request_id=request_id, error_payload=error_payload)
        final_payload = _stream_error_final_payload(error_payload=error_payload, session_id=session_id, request_id=request_id, runtime_events=[runtime_event], settings=request.app.get(SETTINGS_KEY))
        chat_run_registry.fail(request_id, error_payload)
        try:
            await _write_sse(resp, "runtime_event", runtime_event)
            await _write_sse(resp, "error", error_payload)
            await _write_sse(resp, "final", final_payload)
            await _write_sse(resp, "done", {"ok": True})
        except SSEClientDisconnected:
            return resp
    except Exception as exc:
        if not chat_task.done():
            chat_task.cancel()
            await asyncio.gather(chat_task, return_exceptions=True)
        error_payload = {"error": "chat_failed", "detail": safe_preview(str(exc), 500), "session_id": session_id, "request_id": request_id}
        runtime_event = _stream_failure_event(request, session_id=session_id, request_id=request_id, error_payload=error_payload)
        final_payload = _stream_error_final_payload(error_payload=error_payload, session_id=session_id, request_id=request_id, runtime_events=[runtime_event], settings=request.app.get(SETTINGS_KEY))
        chat_run_registry.fail(request_id, error_payload)
        try:
            await _write_sse(resp, "runtime_event", runtime_event)
            await _write_sse(resp, "error", error_payload)
            await _write_sse(resp, "final", final_payload)
            await _write_sse(resp, "done", {"ok": True})
        except SSEClientDisconnected:
            return resp
    finally:
        bus.unsubscribe(subscriber)
    await _safe_write_eof(resp)
    return resp
