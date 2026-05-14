from __future__ import annotations

import asyncio
import hashlib
import json
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
    REQUEST_BINDING_STORE_KEY,
)

from .opencode_client import OpenCodeClientError
from .attachment_service import build_opencode_attachment_parts
from .session_store import SessionDeletedError, SessionRecord
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
    find_latest_assistant_completion,
    extract_assistant_message_ids,
    extract_reasoning_texts_from_parts,
    message_id as adapter_message_id,
    message_role as adapter_message_role,
)
from .skill_invocation import build_skill_prompt, evaluate_skill_invocation, parse_slash_invocation
from .repository_workspace import ensure_repo_checkout, parse_create_pr_repo_request

FINAL_RESPONSE_CONTRACT_SUFFIX = "\n\nRuntime contract: Never end a user-visible answer with only progress text. If work is complete, provide a final answer with summary and evidence. If blocked, state the exact blocker. If more tool work is needed, continue tool work instead of returning a progress-only final."

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


def _append_unique_message_ids(target: list[str], candidates: Iterable[str]) -> None:
    seen = set(target)
    for candidate in candidates:
        if candidate and candidate not in seen:
            target.append(candidate)
            seen.add(candidate)




def extract_assistant_text(payload: Any) -> str:
    return extract_last_assistant_visible_text(payload)


async def _wait_for_assistant_completion(*, client, opencode_session_id: str, response_payload: Any, before_messages: list[dict[str, Any]], timeout_seconds: float, poll_seconds: float, before_snapshot_unreliable: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    before_ids = {adapter_message_id(m) for m in before_messages if adapter_message_id(m)}
    probe = find_latest_assistant_completion(response_payload, exclude_message_ids=before_ids)
    if probe.get("completion_state") == "completed":
        return probe, []
    if probe.get("completion_state") in {"error", "failed"}:
        return probe, []

    loop = asyncio.get_running_loop()
    timeout = max(0.0, float(timeout_seconds))
    sleep_for = max(0.1, float(poll_seconds))
    deadline = loop.time() + timeout
    after_messages: list[dict[str, Any]] = []
    last_probe = probe

    while True:
        after_messages = await client.list_messages(opencode_session_id)
        last_probe = find_latest_assistant_completion(after_messages, exclude_message_ids=before_ids)
        if before_snapshot_unreliable and last_probe.get("completion_state") == "completed":
            last_probe = {
                "text": "",
                "message_id": "",
                "completion_state": "incomplete",
                "reason": "before_snapshot_unreliable",
                "diagnostics": {"before_snapshot_unreliable": True},
            }

        if last_probe.get("completion_state") in {"completed", "error", "blocked"}:
            return last_probe, after_messages

        if loop.time() >= deadline:
            diagnostics = last_probe.get("diagnostics") if isinstance(last_probe.get("diagnostics"), dict) else {}
            preview = diagnostics.get("text") if isinstance(diagnostics.get("text"), str) else ""
            return {"text": "", "message_id": last_probe.get("message_id", ""), "completion_state": "incomplete", "reason": "final_assistant_message_timeout", "diagnostics": {**diagnostics, "progress_preview": safe_preview(preview, 300), "timeout_seconds": timeout, "poll_seconds": sleep_for}}, after_messages

        await asyncio.sleep(sleep_for)


async def _send_skill_prompt(
    *,
    client,
    record,
    parts: list[dict[str, Any]],
    skill_decision,
    invocation,
    model: str | None,
    agent: str | None,
    system: str | None,
    skill_debug: dict[str, Any],
    runtime_events: list[dict[str, Any]],
    bus,
    trace_context: dict[str, str],
    portal_session_id: str,
    request_id: str,
    command_error: str | None = None,
    repository_context: Any | None = None,
    user_message_id: str | None = None,
) -> Any:
    prompt = build_skill_prompt(skill_decision.skill or {}, invocation)
    if repository_context is not None:
        prompt += (
            f"\n\nRepository has been prepared at: {repository_context.path}\n"
            "All local git inspection commands must run from that directory\n"
            f"Use base branch: {repository_context.base_branch}\n"
            f"Use head branch: {repository_context.head_branch}\n"
            "Do not run git inspection from /workspace unless /workspace/.git exists"
        )
    parts[0]["text"] = prompt
    skill_debug["used_skill_prompt"] = True
    if command_error:
        skill_debug["command_execution_error"] = command_error
    response_payload = await client.send_message(
        record.opencode_session_id,
        parts=parts,
        model=model,
        agent=agent,
        system=system,
        message_id=user_message_id,
    )
    prompt_data = {
        "skill": skill_decision.skill.get("opencode_name") if skill_decision.skill else invocation.skill_name
    }
    if skill_debug.get("command_lookup_error"):
        prompt_data["command_lookup_error"] = skill_debug["command_lookup_error"]
    if skill_debug.get("command_execution_error"):
        prompt_data["command_execution_error"] = skill_debug["command_execution_error"]
    prompt_evt = add_trace_context(
        {
            "type": "skill.prompt_applied",
            "session_id": portal_session_id,
            "request_id": request_id,
            "opencode_session_id": record.opencode_session_id,
            "data": prompt_data,
        },
        trace_context,
    )
    runtime_events.append(prompt_evt)
    await bus.publish(prompt_evt)
    return response_payload


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


def _completion_progress_signature(probe: dict[str, Any], assistant_text: str, after_messages: list[dict[str, Any]] | None) -> tuple[str, str, int]:
    message_id = str(probe.get("message_id") or "")
    text_preview = safe_preview(assistant_text or "", 120)
    msg_count = len(after_messages or [])
    return (message_id, text_preview, msg_count)


def _looks_progress_only_text(text: str) -> bool:
    t = (text or "").strip().lower()
    return t.startswith(("i am ", "i'm ", "working", "reading", "let me"))


def _should_auto_continue(state: str, probe: dict[str, Any], assistant_text: str, settings) -> tuple[bool, str]:
    if not settings.chat_auto_continue_enabled:
        return False, "auto_continue_disabled"
    if state in {"blocked", "error", "completed", "empty_final"}:
        return False, "terminal_state"
    if not (assistant_text or "").strip():
        return False, "empty_assistant_text"
    reason = str(probe.get("reason") or "").lower()
    disallow_reasons = {"pending_permission", "tool_error", "provider_error", "auth_error", "cancelled", "user_cancelled"}
    if reason in disallow_reasons:
        return False, f"disallowed_reason:{reason}"
    if reason in {"final_assistant_message_timeout", "length", "continue", "tool_use", "tool_calls"}:
        return True, f"reason:{reason}"
    if state == "incomplete" and _looks_progress_only_text(assistant_text):
        return True, "state_incomplete_progress_text"
    if _looks_progress_only_text(assistant_text):
        return True, "progress_only_text"
    return False, "not_eligible"


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
    if system:
        system = f"{system}{FINAL_RESPONSE_CONTRACT_SUFFIX}"
    else:
        system = FINAL_RESPONSE_CONTRACT_SUFFIX.strip()
    attachments = payload.get("attachments")

    store = request.app[SESSION_STORE_KEY]
    bus = request.app[EVENT_BUS_KEY]
    client = request.app[OPENCODE_CLIENT_KEY]
    chatlog_store = request.app[CHATLOG_STORE_KEY]
    usage_tracker = request.app[USAGE_TRACKER_KEY]
    portal_metadata_client = request.app[PORTAL_METADATA_CLIENT_KEY]
    settings = request.app[SETTINGS_KEY]
    binding_store = request.app.get(REQUEST_BINDING_STORE_KEY)

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

        if binding_store is not None:
            binding_store.bind_active(record.opencode_session_id, portal_session_id, request_id, kind="chat")
        start = add_trace_context(chat_started_event(session_id=portal_session_id, request_id=request_id, opencode_session_id=record.opencode_session_id), trace_context)
        runtime_events.append(start)
        await bus.publish(start)

        chatlog_store.start_entry(portal_session_id, request_id=request_id, message=message, runtime_events=runtime_events, context_state=context_state, llm_debug={"engine": "opencode", "opencode_session_id": record.opencode_session_id, "trace_context": trace_context})

        await portal_metadata_client.publish_session_metadata(session_id=portal_session_id, latest_event_type="chat.started", latest_event_state="running", request_id=request_id, summary="Chat started", runtime_events=runtime_events, metadata={"opencode_session_id": record.opencode_session_id, "trace_context": trace_context})

        think = add_trace_context(llm_thinking_event(session_id=portal_session_id, request_id=request_id, opencode_session_id=record.opencode_session_id), trace_context)
        runtime_events.append(think)
        await bus.publish(think)

        before_messages: list[dict[str, Any]] = []
        after_messages: list[dict[str, Any]] = []
        before_assistant_message_ids: set[str] = set()
        message_id_detection_error_before = ""
        message_id_detection_error_after = ""
        before_snapshot_unreliable = False
        try:
            before_messages = await client.list_messages(record.opencode_session_id)
            before_assistant_message_ids = set(extract_assistant_message_ids(before_messages))
        except Exception as exc:
            message_id_detection_error_before = safe_preview(str(exc), 500)
            before_snapshot_unreliable = True
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

        invocation = parse_slash_invocation(message)
        skill_debug = None
        initial_user_message_id = payload.get("message_id") if isinstance(payload.get("message_id"), str) and payload.get("message_id", "").strip() else f"portal-user-{request_id}"
        if binding_store is not None:
            binding_store.bind_message(record.opencode_session_id, initial_user_message_id, portal_session_id, request_id, kind="chat")
        if invocation:
            skill_decision = evaluate_skill_invocation(settings, invocation)
            executed_native_command = False
            skill_detected = add_trace_context({"type": "skill.detected", "session_id": portal_session_id, "request_id": request_id, "opencode_session_id": record.opencode_session_id, "data": {"raw_name": invocation.raw_name, "skill_name": invocation.skill_name, "arguments": invocation.arguments}}, trace_context)
            runtime_events.append(skill_detected)
            await bus.publish(skill_detected)
            skill_debug = {"kind": "skill", "raw_name": invocation.raw_name, "skill_name": invocation.skill_name, "arguments": invocation.arguments, "reason": skill_decision.reason, "permission_state": skill_decision.permission_state, "used_command_api": False, "used_skill_prompt": False, "blocked": False}
            if skill_decision.reason == "unknown_skill":
                try:
                    available_commands = await client.list_commands()
                except Exception as exc:
                    skill_decision = type(skill_decision)(skill=None, allowed=False, reason="command_lookup_failed", permission_state="unknown")
                    skill_debug["reason"] = "command_lookup_failed"
                    skill_debug["command_lookup_error"] = safe_preview(str(exc), 300)
                else:
                    command_names = {str(c.get("name") or "") for c in available_commands if isinstance(c, dict)}
                    if invocation.skill_name in command_names:
                        try:
                            response_payload = await client.execute_command(record.opencode_session_id, command=invocation.skill_name, arguments=invocation.arguments, model=model, agent=agent or "efp-main")
                            skill_debug.update({"kind": "command", "used_command_api": True, "native_command": True, "reason": "allowed"})
                            executed_native_command = True
                            command_evt = add_trace_context({"type": "skill.command.executed", "session_id": portal_session_id, "request_id": request_id, "opencode_session_id": record.opencode_session_id, "data": {"command": invocation.skill_name, "native_command": True}}, trace_context)
                            runtime_events.append(command_evt)
                            await bus.publish(command_evt)
                        except OpenCodeClientError as exc:
                            skill_decision = type(skill_decision)(skill=None, allowed=False, reason="command_execution_failed", permission_state="unknown")
                            skill_debug["reason"] = "command_execution_failed"
                            skill_debug["command_execution_error"] = safe_preview(str(exc), 300)
                    else:
                        skill_decision = type(skill_decision)(skill=None, allowed=False, reason="unknown_skill_or_command", permission_state="unknown")
                        skill_debug["reason"] = "unknown_skill_or_command"
            elif not skill_decision.allowed:
                pass

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
                    evt_type = "skill.repository_checkout.completed" if checkout_result.success else "skill.repository_checkout.failed"
                    repo_evt = add_trace_context({"type": evt_type, "session_id": portal_session_id, "request_id": request_id, "opencode_session_id": record.opencode_session_id, "data": {"path": checkout_result.path, "owner": checkout_result.owner, "repo": checkout_result.repo, "head_branch": checkout_result.head_branch, "base_branch": checkout_result.base_branch, "error": checkout_result.error}}, trace_context)
                    runtime_events.append(repo_evt)
                    await bus.publish(repo_evt)
                    if checkout_result.success:
                        repository_context = checkout_result
                    else:
                        skill_decision = type(skill_decision)(skill=skill_decision.skill, allowed=False, reason="repository_checkout_failed", permission_state=skill_decision.permission_state)
                        skill_debug["reason"] = "repository_checkout_failed"

            if (not skill_decision.allowed) and (not executed_native_command):
                assistant_text = f"Skill `{invocation.skill_name}` cannot run in OpenCode runtime: {skill_decision.reason}."
                if skill_decision.reason == "missing_required_writeback_tools":
                    assistant_text = "Skill create-pull-request cannot run because required writeback tool github_create_pull_request / efp_github_create_pull_request is unavailable."
                elif skill_decision.reason == "repository_checkout_failed" and repo_request is not None:
                    preflight = skill_debug.get("repository_preflight", {})
                    assistant_text = (
                        f"Repository checkout failed for {repo_request.repo_url} to {preflight.get('path', '')} "
                        f"(head: {repo_request.head_branch}, base: {repo_request.base_branch}, failure: {preflight.get('error')})."
                    )
                skill_debug["blocked"] = True
                skill_debug["kind"] = "command" if skill_decision.reason in {"unknown_skill_or_command", "command_lookup_failed", "command_execution_failed"} else "skill"
                blocked_evt = add_trace_context({"type": "skill.blocked", "session_id": portal_session_id, "request_id": request_id, "opencode_session_id": record.opencode_session_id, "data": {"reason": skill_decision.reason, "permission_state": skill_decision.permission_state}}, trace_context)
                runtime_events.append(blocked_evt)
                await bus.publish(blocked_evt)
                blocked_chat_evt = add_trace_context({"type": "chat.blocked", "event_type": "chat.blocked", "state": "blocked", "ok": False, "completion_state": "blocked", "session_id": portal_session_id, "request_id": request_id, "opencode_session_id": record.opencode_session_id, "data": {"reason": skill_decision.reason, "permission_state": skill_decision.permission_state}}, trace_context)
                for event in [blocked_chat_evt]:
                    runtime_events.append(event)
                    await bus.publish(event)
                final_context = {**context_state, "summary": assistant_text[:500], "current_state": "blocked", "next_step": ""}
                updated = store.update_after_chat(portal_session_id, message, assistant_text, model, agent)
                usage_record = usage_tracker.record_chat(session_id=portal_session_id, request_id=request_id, model=model, provider=provider_for_trace, response_payload={}, input_text=message, output_text=assistant_text)
                usage_record["request_id"] = trace_context.get("request_id", usage_record.get("request_id", ""))
                completion_probe = {"completion_state": "blocked", "reason": "skill_blocked", "diagnostics": {"skill_name": invocation.skill_name, "permission_state": skill_decision.permission_state, "reason": skill_decision.reason}}
                llm_debug = {"engine": "opencode", "opencode_session_id": updated.opencode_session_id, "usage": usage_record, "trace_context": trace_context, "attachments": attachment_debug, "skill_invocation": skill_debug, "completion_probe": completion_probe}
                chatlog_store.finish_entry(portal_session_id, request_id=request_id, status="blocked", response=assistant_text, runtime_events=runtime_events, events=runtime_events, context_state=final_context, llm_debug=llm_debug)
                if not updated.deleted:
                    await portal_metadata_client.publish_session_metadata(session_id=portal_session_id, latest_event_type="chat.completed", latest_event_state="blocked", request_id=request_id, summary=assistant_text[:300], runtime_events=runtime_events, metadata={"engine": "opencode", "opencode_session_id": updated.opencode_session_id, "context_state": final_context, "usage": usage_record, "trace_context": trace_context, "skill_invocation": skill_debug})
                return {"ok": False, "completion_state": "blocked", "incomplete_reason": skill_decision.reason or "skill_blocked", "session_id": portal_session_id, "request_id": trace_context.get("request_id", request_id), "response": assistant_text, "user_message_id": "", "assistant_message_id": "", "assistant_message_ids": [], "events": runtime_events, "runtime_events": runtime_events, "usage": usage_record, "context_state": final_context, "_llm_debug": llm_debug}

            if not executed_native_command:
                command_names: set[str] = set()
                if not attachments:
                    try:
                        available_commands = await client.list_commands()
                        command_names = {str(c.get("name") or "") for c in available_commands if isinstance(c, dict)}
                    except Exception as exc:
                        skill_debug["command_lookup_error"] = safe_preview(str(exc), 300)
                if (not attachments) and str(skill_decision.skill.get("opencode_name") or "") in command_names:
                    try:
                        response_payload = await client.execute_command(record.opencode_session_id, command=str(skill_decision.skill["opencode_name"]), arguments=invocation.arguments, model=model, agent=agent or "efp-main")
                        skill_debug["used_command_api"] = True
                        skill_debug["kind"] = "skill"
                        command_evt = add_trace_context({"type": "skill.command.executed", "session_id": portal_session_id, "request_id": request_id, "opencode_session_id": record.opencode_session_id, "data": {"command": skill_decision.skill["opencode_name"]}}, trace_context)
                        runtime_events.append(command_evt)
                        await bus.publish(command_evt)
                    except OpenCodeClientError as exc:
                        error_preview = safe_preview(str(exc), 300)
                        skill_debug["command_execution_error"] = error_preview
                        skill_debug["used_command_api"] = False
                        skill_debug["command_api_fallback"] = True
                        command_failed_evt = add_trace_context({"type": "skill.command.failed", "session_id": portal_session_id, "request_id": request_id, "opencode_session_id": record.opencode_session_id, "data": {"command": skill_decision.skill["opencode_name"], "error": error_preview, "fallback": "skill_prompt"}}, trace_context)
                        runtime_events.append(command_failed_evt)
                        await bus.publish(command_failed_evt)
                        response_payload = await _send_skill_prompt(
                            client=client,
                            record=record,
                            parts=parts,
                            skill_decision=skill_decision,
                            invocation=invocation,
                            model=model,
                            agent=agent,
                            system=system,
                            skill_debug=skill_debug,
                            runtime_events=runtime_events,
                            bus=bus,
                            trace_context=trace_context,
                            portal_session_id=portal_session_id,
                            request_id=request_id,
                            command_error=error_preview,
                            repository_context=repository_context,
                            user_message_id=initial_user_message_id,
                        )
                else:
                    response_payload = await _send_skill_prompt(
                        client=client,
                        record=record,
                        parts=parts,
                        skill_decision=skill_decision,
                        invocation=invocation,
                        model=model,
                        agent=agent,
                        system=system,
                        skill_debug=skill_debug,
                        runtime_events=runtime_events,
                        bus=bus,
                        trace_context=trace_context,
                        portal_session_id=portal_session_id,
                        request_id=request_id,
                        repository_context=repository_context,
                        user_message_id=initial_user_message_id,
                    )
        else:
            response_payload = await client.send_message(record.opencode_session_id, parts=parts, model=model, agent=agent, system=system, message_id=initial_user_message_id)
        completion_probe, waited_messages = await _wait_for_assistant_completion(
            client=client,
            opencode_session_id=record.opencode_session_id,
            response_payload=response_payload,
            before_messages=before_messages,
            timeout_seconds=settings.chat_completion_timeout_seconds,
            poll_seconds=settings.chat_completion_poll_seconds,
            before_snapshot_unreliable=before_snapshot_unreliable,
        )
        continuation_count = 0
        continuation_debug: list[dict[str, Any]] = []
        completion_state = str(completion_probe.get("completion_state") or "incomplete")
        assistant_text = str(completion_probe.get("text") or extract_assistant_text(response_payload) or "")
        incomplete_reason = str(completion_probe.get("reason") or "")
        last_sig = _completion_progress_signature(completion_probe, assistant_text, waited_messages)
        allow_continue, continue_reason = _should_auto_continue(completion_state, completion_probe, assistant_text, settings)
        while continuation_count < settings.chat_auto_continue_max_turns and allow_continue:
            continuation_count += 1
            cont_id = f"efp-auto-continue-{request_id}-{continuation_count}"
            if binding_store is not None:
                binding_store.bind_message(record.opencode_session_id, cont_id, portal_session_id, request_id, kind="continuation")
            cont_evt = add_trace_context({"type":"continuation.started","session_id":portal_session_id,"request_id":request_id,"opencode_session_id":record.opencode_session_id,"data":{"index":continuation_count}}, trace_context)
            runtime_events.append(cont_evt); await bus.publish(cont_evt)
            before_messages = await client.list_messages(record.opencode_session_id)
            try:
                continuation_response_payload = await client.send_message(record.opencode_session_id, parts=[{"type":"text","text":settings.chat_auto_continue_prompt}], model=model, agent=agent, system=system, message_id=cont_id)
                completion_probe, after_messages = await _wait_for_assistant_completion(client=client, opencode_session_id=record.opencode_session_id, response_payload=continuation_response_payload, before_messages=before_messages, timeout_seconds=settings.chat_completion_timeout_seconds, poll_seconds=settings.chat_completion_poll_seconds)
            except Exception as exc:
                failed_evt = add_trace_context({"type":"continuation.failed","session_id":portal_session_id,"request_id":request_id,"opencode_session_id":record.opencode_session_id,"data":{"index":continuation_count,"error":safe_preview(str(exc),300)}}, trace_context)
                runtime_events.append(failed_evt); await bus.publish(failed_evt)
                completion_state = "incomplete"
                incomplete_reason = "auto_continue_failed"
                break
            assistant_text = str(completion_probe.get("text") or extract_assistant_text(continuation_response_payload) or assistant_text)
            completion_state = str(completion_probe.get("completion_state") or completion_state)
            continuation_debug.append({"index": continuation_count, "completion_state": completion_state, "reason": completion_probe.get("reason"), "message_id": cont_id, "text_preview": safe_preview(assistant_text, 200)})
            done_evt = add_trace_context({"type":"continuation.completed","session_id":portal_session_id,"request_id":request_id,"opencode_session_id":record.opencode_session_id,"data":{"index":continuation_count,"completion_state":completion_state}}, trace_context)
            runtime_events.append(done_evt); await bus.publish(done_evt)
            new_sig = _completion_progress_signature(completion_probe, assistant_text, after_messages)
            if settings.chat_auto_continue_no_progress_stop and assistant_text.strip() and new_sig == last_sig:
                incomplete_reason = "auto_continue_no_progress"
                allow_continue = False
                completion_state = "incomplete"
                break
            last_sig = new_sig
            allow_continue, continue_reason = _should_auto_continue(completion_state, completion_probe, assistant_text, settings)
            incomplete_reason = str(completion_probe.get("reason") or continue_reason or "")
        if completion_state != "completed" and continuation_count >= settings.chat_auto_continue_max_turns and allow_continue:
            completion_state = "incomplete"
            incomplete_reason = "auto_continue_max_turns_reached"
        if completion_state != "completed" and not incomplete_reason:
            incomplete_reason = str(completion_probe.get("reason") or "assistant_completion_not_final")
        payload_message = response_payload.get("message") if isinstance(response_payload, dict) else None
        if isinstance(payload_message, dict):
            reasoning_texts = extract_reasoning_texts_from_parts(payload_message.get("parts"))
            for item in reasoning_texts:
                think_event = add_trace_context({"type": "llm_thinking", "engine": "opencode", "session_id": portal_session_id, "request_id": request_id, "opencode_session_id": record.opencode_session_id, "message": safe_preview(item, 300), "data": {"message": safe_preview(item, 300)}, "created_at": utc_now_iso()}, trace_context)
                runtime_events.append(think_event)
                await bus.publish(think_event)
        user_message_id = initial_user_message_id
        assistant_message_id = ""
        assistant_message_ids: list[str] = []
        if not before_snapshot_unreliable:
            try:
                after_messages = waited_messages or await client.list_messages(record.opencode_session_id)
                user_message_id, assistant_message_id = _detect_new_message_ids(before_messages, after_messages)
                _append_unique_message_ids(
                    assistant_message_ids,
                    extract_assistant_message_ids(after_messages, exclude_message_ids=before_assistant_message_ids),
                )
            except Exception as exc:
                message_id_detection_error_after = safe_preview(str(exc), 500)
        if completion_state == "completed" and not assistant_text.strip():
            completion_state = "empty_final"
            incomplete_reason = "empty_final_assistant_text"
            assistant_text = "OpenCode completed without a visible assistant response. The request may have produced tool output only; no empty success message was recorded."
        if not assistant_message_id and isinstance(response_payload, dict):
            candidate = response_payload.get("info", {}).get("id") if isinstance(response_payload.get("info"), dict) else ""
            if not candidate and isinstance(response_payload.get("message"), dict):
                candidate = adapter_message_id(response_payload["message"])
            assistant_message_id = str(candidate or "")
        _append_unique_message_ids(
            assistant_message_ids,
            extract_assistant_message_ids(response_payload, exclude_message_ids=before_assistant_message_ids),
        )
        probe_message_id = str(completion_probe.get("message_id") or "")
        _append_unique_message_ids(assistant_message_ids, [probe_message_id, assistant_message_id])
        if assistant_message_ids:
            assistant_message_id = assistant_message_ids[-1]
        elif assistant_message_id:
            assistant_message_ids = [assistant_message_id]
        if binding_store is not None:
            for _aid in [assistant_message_id, *assistant_message_ids]:
                if _aid:
                    binding_store.bind_message(record.opencode_session_id, _aid, portal_session_id, request_id, kind="assistant")
        if skill_debug and not skill_debug.get("blocked"):
            completed_evt = add_trace_context({"type": "skill.completed", "session_id": portal_session_id, "request_id": request_id, "opencode_session_id": record.opencode_session_id, "data": {"skill": skill_debug.get("skill_name"), "kind": skill_debug.get("kind", "skill"), "used_command_api": bool(skill_debug.get("used_command_api")), "used_skill_prompt": bool(skill_debug.get("used_skill_prompt"))}}, trace_context)
            runtime_events.append(completed_evt)
            await bus.publish(completed_evt)

        if completion_state == "completed":
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
            status = "success"; latest_state = "success"; ok = True
            final_context = {**context_state, "summary": assistant_text[:500], "current_state": "completed", "next_step": ""}
        elif completion_state == "blocked":
            assistant_text = "OpenCode is blocked waiting for a permission/tool decision before producing a final answer."
            out_events = [{"type": "chat.blocked", "event_type": "chat.blocked", "state": "blocked", "session_id": portal_session_id, "request_id": request_id, "opencode_session_id": record.opencode_session_id, "data": completion_probe.get("diagnostics", {})}]
            status = "blocked"; latest_state = "blocked"; ok = False
            final_context = {**context_state, "summary": assistant_text[:500], "current_state": "blocked", "next_step": ""}
        elif completion_state == "empty_final":
            out_events = [{"type": "chat.empty_final", "event_type": "chat.empty_final", "state": "empty_final", "session_id": portal_session_id, "request_id": request_id, "opencode_session_id": record.opencode_session_id, "data": completion_probe.get("diagnostics", {})}]
            status = "empty_final"; latest_state = "empty_final"; ok = False
            final_context = {**context_state, "summary": assistant_text[:500], "current_state": "empty_final", "next_step": ""}
        elif completion_state == "error":
            reason = completion_probe.get("diagnostics", {}).get("error_summary") if isinstance(completion_probe.get("diagnostics"), dict) else ""
            assistant_text = f"OpenCode tool execution failed before a final answer was produced: {reason or 'unknown tool error'}."
            out_events = [chat_failed_event(session_id=portal_session_id, request_id=request_id, opencode_session_id=record.opencode_session_id, error=assistant_text)]
            status = "error"; latest_state = "error"; ok = False
            final_context = {**context_state, "summary": assistant_text[:500], "current_state": "error", "next_step": ""}
        else:
            completion_state = "incomplete"
            assistant_text = "OpenCode stream ended before a final assistant response was available. The last visible text was only an intermediate progress update."
            out_events = [{"type": "chat.incomplete", "event_type": "chat.incomplete", "state": "incomplete", "session_id": portal_session_id, "request_id": request_id, "opencode_session_id": record.opencode_session_id, "data": completion_probe.get("diagnostics", {})}]
            status = "incomplete"; latest_state = "incomplete"; ok = False
            final_context = {**context_state, "summary": assistant_text[:500], "current_state": "incomplete", "next_step": ""}
        for event in [add_trace_context(x, trace_context) for x in out_events]:
            runtime_events.append(event)
            await bus.publish(event)

        updated = store.update_after_chat(portal_session_id, message, assistant_text, model, agent)
        provider = provider_for_trace
        usage_record = usage_tracker.record_chat(session_id=portal_session_id, request_id=request_id, model=model, provider=provider, response_payload=response_payload, input_text=message, output_text=assistant_text)
        usage_record["request_id"] = trace_context.get("request_id", usage_record.get("request_id", ""))

        llm_debug = {"engine": "opencode", "opencode_session_id": updated.opencode_session_id, "usage": usage_record, "response_payload_preview": safe_preview(_redact_attachment_payloads_for_debug(response_payload), 2000), "trace_context": trace_context, "message_ids": {"user_message_id": user_message_id or "", "assistant_message_id": assistant_message_id or "", "assistant_message_ids": assistant_message_ids}, "attachments": attachment_debug}
        if skill_debug:
            llm_debug["skill_invocation"] = skill_debug
        if message_id_detection_error_before:
            llm_debug["message_id_detection_error_before"] = message_id_detection_error_before
        if message_id_detection_error_after:
            llm_debug["message_id_detection_error_after"] = message_id_detection_error_after
        chatlog_store.finish_entry(portal_session_id, request_id=request_id, status=status, response=assistant_text, runtime_events=runtime_events, events=runtime_events, context_state=final_context, llm_debug=llm_debug)

        metadata_model = usage_record.get("model") or model or "unknown"
        metadata_provider = usage_record.get("provider") or provider or "unknown"
        if not updated.deleted:
            await portal_metadata_client.publish_session_metadata(session_id=portal_session_id, latest_event_type="chat.completed", latest_event_state=latest_state, request_id=request_id, summary=assistant_text[:300], runtime_events=runtime_events, metadata={"engine": "opencode", "opencode_session_id": updated.opencode_session_id, "model": metadata_model, "provider": metadata_provider, "context_state": final_context, "usage": usage_record, "trace_context": trace_context})

    except SessionDeletedError:
        raise web.HTTPGone(text=json.dumps({"error": "session_deleted", "session_id": portal_session_id, "detail": "Session was deleted"}), content_type="application/json")
    except OpenCodeClientError as exc:
        if not trace_context:
            trace_context = build_trace_context(settings, request_id=request_id, session_id=portal_session_id, opencode_session_id=opencode_session_id, profile_version=profile_version, runtime_profile_id=runtime_profile_id, model=model or "", provider=provider_for_trace or "")
        failed = add_trace_context(chat_failed_event(session_id=portal_session_id, request_id=request_id, opencode_session_id=opencode_session_id, error=str(exc)), trace_context)
        runtime_events.append(failed)
        await bus.publish(failed)
        chatlog_store.fail_entry(portal_session_id, request_id=request_id, error=str(exc), runtime_events=runtime_events, context_state=context_state, llm_debug={"engine": "opencode", "opencode_session_id": opencode_session_id, "trace_context": trace_context})
        await portal_metadata_client.publish_session_metadata(session_id=portal_session_id, latest_event_type="chat.failed", latest_event_state="error", request_id=request_id, summary=str(exc), runtime_events=runtime_events, metadata={"engine": "opencode", "trace_context": trace_context})
        raise web.HTTPBadGateway(text=json.dumps({"error": "opencode_error", "detail": str(exc)}), content_type="application/json")

    out = {"ok": ok, "completion_state": completion_state, "incomplete_reason": incomplete_reason, "session_id": portal_session_id, "request_id": trace_context.get("request_id", request_id), "response": assistant_text, "user_message_id": user_message_id or "", "assistant_message_id": assistant_message_id or "", "assistant_message_ids": assistant_message_ids, "events": runtime_events, "runtime_events": runtime_events, "usage": usage_record, "continuation_count": continuation_count, "auto_continue_enabled": settings.chat_auto_continue_enabled, "context_state": final_context, "_llm_debug": {"engine": "opencode", "opencode_session_id": updated.opencode_session_id, "usage": usage_record, "thinking_events": runtime_events, "trace_context": trace_context, "attachments": attachment_debug, "completion_probe": completion_probe, "message_ids": {"user_message_id": user_message_id or "", "assistant_message_id": assistant_message_id or "", "assistant_message_ids": assistant_message_ids}, "continuations": continuation_debug}}
    if message_id_detection_error_before:
        out["_llm_debug"]["message_id_detection_error_before"] = message_id_detection_error_before
    if message_id_detection_error_after:
        out["_llm_debug"]["message_id_detection_error_after"] = message_id_detection_error_after
    if 'skill_debug' in locals() and skill_debug:
        out["_llm_debug"]["skill_invocation"] = skill_debug
    if partial_recovery or getattr(updated, "partial_recovery", False):
        out["_llm_debug"]["partial_recovery"] = True
    if binding_store is not None:
        binding_store.complete(request_id)
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
BRIDGE_EVENT_TYPES = {"tool.started", "tool.completed", "tool.failed", "permission_request", "permission_resolved", "assistant_delta", "message.delta", "llm_thinking", "opencode.reasoning", "provider.retry", "provider.status", "execution.started", "execution.completed", "execution.failed", "complete", "final", "error", "skill.detected", "skill.blocked", "skill.command.executed", "skill.command.failed", "skill.prompt_applied", "skill.completed", "skill.repository_checkout.completed", "skill.repository_checkout.failed"}


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
    binding_store = request.app.get(REQUEST_BINDING_STORE_KEY)
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
                and (role == "assistant" or bool(data.get("metadata_incomplete")))
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
            error_payload = {"error": "chat_failed", "detail": exc.text, "session_id": session_id, "request_id": req_id}
        except Exception as exc:
            error_payload = {"error": "chat_failed", "detail": safe_preview(str(exc), 500), "session_id": session_id, "request_id": req_id}

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
            await _write_sse(resp, "done", {"ok": True})
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
        if not client_disconnected:
            await _safe_write_eof(resp)
    return resp
