"""Context usage and manual compaction endpoints for the OpenCode adapter."""

from __future__ import annotations

import json
import math
from typing import Any, Mapping

from aiohttp import web

from .app_keys import OPENCODE_CLIENT_KEY, SESSION_STORE_KEY
from .opencode_client import OpenCodeClientError
from .opencode_config import normalize_opencode_provider_id
from .thinking_events import utc_now_iso


_LABELS = {
    "instructions": "Instructions",
    "tool_definitions": "Tool definitions",
    "conversation": "Conversation",
    "tool_activity": "Tool activity",
}
_TOOL_PART_TYPES = {
    "attachment",
    "file",
    "patch",
    "tool",
    "tool-call",
    "tool-result",
    "tool_call",
    "tool_result",
}
_BUSY_STATES = {"busy", "in_progress", "pending", "queued", "retry", "running"}


def _estimate_tokens(value: Any) -> int:
    if value is None or value == "" or value == [] or value == {}:
        return 0
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        encoded = str(value or "")
    return max(1, int(math.ceil(len(encoded) / 4))) if encoded else 0


def _safe_int(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _message_info(message: Mapping[str, Any]) -> Mapping[str, Any]:
    info = message.get("info")
    return info if isinstance(info, Mapping) else message


def _message_parts(message: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    parts = message.get("parts")
    if not isinstance(parts, list):
        return []
    return [part for part in parts if isinstance(part, Mapping)]


def _model_ref(messages: list[dict[str, Any]], record: Any) -> tuple[str | None, str | None]:
    for message in reversed(messages):
        info = _message_info(message)
        model = info.get("model") or message.get("model")
        if isinstance(model, Mapping):
            provider_id = model.get("providerID") or model.get("provider_id")
            model_id = model.get("modelID") or model.get("model_id")
            if provider_id and model_id:
                return normalize_opencode_provider_id(str(provider_id)), str(model_id)
        provider_id = info.get("providerID") or info.get("provider_id") or message.get("provider")
        model_id = info.get("modelID") or info.get("model_id")
        if provider_id and model_id:
            return normalize_opencode_provider_id(str(provider_id)), str(model_id)
    configured = str(getattr(record, "model", "") or "").strip()
    if "/" in configured:
        provider_id, model_id = configured.split("/", 1)
        if provider_id.strip() and model_id.strip():
            return normalize_opencode_provider_id(provider_id.strip()), model_id.strip()
    return None, None


def _latest_actual_input_tokens(messages: list[dict[str, Any]]) -> int:
    for message in reversed(messages):
        info = _message_info(message)
        if str(info.get("role") or "").strip().lower() != "assistant":
            continue
        tokens = info.get("tokens") or message.get("tokens") or message.get("usage")
        if not isinstance(tokens, Mapping):
            continue
        total = _safe_int(
            tokens.get("input")
            or tokens.get("input_tokens")
            or tokens.get("prompt_tokens")
        )
        cache = tokens.get("cache")
        if isinstance(cache, Mapping):
            total += _safe_int(cache.get("read")) + _safe_int(cache.get("write"))
        total += _safe_int(tokens.get("cache_read_tokens"))
        total += _safe_int(tokens.get("cache_write_tokens"))
        if total:
            return total
    return 0


def _history_estimates(messages: list[dict[str, Any]]) -> tuple[int, int]:
    conversation = 0
    tool_activity = 0
    for message in messages:
        info = _message_info(message)
        conversation += _estimate_tokens(
            {"role": info.get("role") or message.get("role") or "unknown"}
        )
        for part in _message_parts(message):
            part_type = str(part.get("type") or "").strip().lower()
            if part_type in _TOOL_PART_TYPES or "tool" in part_type:
                tool_activity += _estimate_tokens(part)
            elif part_type not in {"step-start", "step-finish", "snapshot"}:
                conversation += _estimate_tokens(part)
    return conversation, tool_activity


def _context_window_tokens(
    providers: Mapping[str, Any] | None,
    provider_id: str | None,
    model_id: str | None,
) -> int | None:
    if not providers or not provider_id or not model_id:
        return None
    all_providers = providers.get("all")
    if isinstance(all_providers, Mapping):
        candidates = list(all_providers.values())
    elif isinstance(all_providers, list):
        candidates = all_providers
    else:
        candidates = []
    for provider in candidates:
        if not isinstance(provider, Mapping):
            continue
        candidate_id = str(provider.get("id") or provider.get("providerID") or "")
        if normalize_opencode_provider_id(candidate_id) != provider_id:
            continue
        models = provider.get("models")
        if isinstance(models, Mapping):
            model = models.get(model_id)
        elif isinstance(models, list):
            model = next(
                (
                    item
                    for item in models
                    if isinstance(item, Mapping)
                    and str(item.get("id") or item.get("modelID") or "") == model_id
                ),
                None,
            )
        else:
            model = None
        if not isinstance(model, Mapping):
            return None
        limit = model.get("limit")
        limit = limit if isinstance(limit, Mapping) else {}
        parsed = _safe_int(limit.get("context") or model.get("contextWindow"))
        return parsed or None
    return None


def _percent(numerator: int, denominator: int | None) -> float | None:
    if not denominator or denominator <= 0:
        return None
    return round((numerator / denominator) * 100.0, 1)


def _scaled_categories(
    *,
    actual_input_tokens: int,
    conversation_tokens: int,
    tool_activity_tokens: int,
    tool_definition_tokens: int,
) -> dict[str, int]:
    known = conversation_tokens + tool_activity_tokens + tool_definition_tokens
    if actual_input_tokens <= 0:
        return {
            "instructions": 0,
            "tool_definitions": tool_definition_tokens,
            "conversation": conversation_tokens,
            "tool_activity": tool_activity_tokens,
        }
    if known <= actual_input_tokens:
        return {
            "instructions": actual_input_tokens - known,
            "tool_definitions": tool_definition_tokens,
            "conversation": conversation_tokens,
            "tool_activity": tool_activity_tokens,
        }
    scale = actual_input_tokens / known if known else 0
    scaled = {
        "instructions": 0,
        "tool_definitions": int(round(tool_definition_tokens * scale)),
        "conversation": int(round(conversation_tokens * scale)),
        "tool_activity": int(round(tool_activity_tokens * scale)),
    }
    drift = actual_input_tokens - sum(scaled.values())
    scaled["conversation"] = max(0, scaled["conversation"] + drift)
    return scaled


async def _busy_state(client: Any, opencode_session_id: str) -> tuple[bool, str | None]:
    if not hasattr(client, "get_session_status"):
        return False, None
    try:
        payload = await client.get_session_status(timeout_seconds=30)
    except TypeError:
        payload = await client.get_session_status()
    except Exception:
        return False, None
    if not isinstance(payload, Mapping):
        return False, None
    sessions = payload.get("sessions")
    candidates = sessions if isinstance(sessions, Mapping) else payload
    state_payload = candidates.get(opencode_session_id) if isinstance(candidates, Mapping) else None
    if isinstance(state_payload, Mapping):
        state = str(
            state_payload.get("type")
            or state_payload.get("state")
            or state_payload.get("status")
            or ""
        ).strip().lower()
    else:
        state = str(state_payload or "").strip().lower()
    return state in _BUSY_STATES, state or None


async def _build_snapshot(client: Any, record: Any, messages: list[dict[str, Any]]) -> dict[str, Any]:
    provider_id, model_id = _model_ref(messages, record)
    conversation_tokens, tool_activity_tokens = _history_estimates(messages)
    tools: list[dict[str, Any]] = []
    providers: Mapping[str, Any] | None = None
    tools_available = False
    if provider_id and model_id and hasattr(client, "list_tools"):
        try:
            tools = await client.list_tools(provider_id, model_id, timeout_seconds=30)
            tools_available = True
        except Exception:
            tools = []
    if hasattr(client, "list_providers"):
        try:
            candidate = await client.list_providers(timeout_seconds=30)
            providers = candidate if isinstance(candidate, Mapping) else None
        except Exception:
            providers = None
    tool_definition_tokens = _estimate_tokens(tools)
    actual_input_tokens = _latest_actual_input_tokens(messages)
    categories_by_id = _scaled_categories(
        actual_input_tokens=actual_input_tokens,
        conversation_tokens=conversation_tokens,
        tool_activity_tokens=tool_activity_tokens,
        tool_definition_tokens=tool_definition_tokens,
    )
    used_tokens = actual_input_tokens or sum(categories_by_id.values())
    context_window_tokens = _context_window_tokens(
        providers, provider_id, model_id
    )
    categories = [
        {
            "id": category_id,
            "label": label,
            "tokens": categories_by_id[category_id],
            "percent_of_used": _percent(categories_by_id[category_id], used_tokens),
            "percent_of_window": _percent(
                categories_by_id[category_id], context_window_tokens
            ),
        }
        for category_id, label in _LABELS.items()
    ]
    method = "opencode_input_tokens_with_local_category_estimate"
    if not actual_input_tokens:
        method = "local_message_character_estimate"
    if not tools_available:
        method += "_without_tool_schema"
    return {
        "engine": "opencode",
        "scope": "last_request" if actual_input_tokens else "current_estimate",
        "precision": "coarse",
        "measurement_method": method,
        "used_tokens": used_tokens,
        "context_window_tokens": context_window_tokens,
        "usage_percent": _percent(used_tokens, context_window_tokens),
        "categories": categories,
        "model": {
            "provider_id": provider_id,
            "model_id": model_id,
            "context_window_tokens": context_window_tokens,
        },
        "updated_at": utc_now_iso(),
    }


async def context_usage_handler(request: web.Request) -> web.Response:
    session_id = request.match_info["session_id"]
    store = request.app[SESSION_STORE_KEY]
    client = request.app[OPENCODE_CLIENT_KEY]
    record = store.get(session_id)
    if not record or record.deleted:
        return web.json_response({"error": "session_not_found"}, status=404)
    try:
        messages = await client.list_messages(record.opencode_session_id)
        snapshot = await _build_snapshot(client, record, messages)
        busy, state = await _busy_state(client, record.opencode_session_id)
    except OpenCodeClientError as exc:
        return web.json_response(
            {"error": "opencode_error", "detail": str(exc)}, status=502
        )
    eligible = len(messages) > 1 and not busy
    reason = None
    if busy:
        reason = "A response is currently running."
    elif len(messages) <= 1:
        reason = "There is not enough conversation history to compact."
    snapshot.update(
        {
            "success": True,
            "session_id": session_id,
            "compact": {
                "supported": True,
                "in_progress": busy,
                "eligible": eligible,
                "reason": reason,
                "runtime_state": state,
            },
        }
    )
    return web.json_response(snapshot)


async def compact_session_handler(request: web.Request) -> web.Response:
    session_id = request.match_info["session_id"]
    store = request.app[SESSION_STORE_KEY]
    client = request.app[OPENCODE_CLIENT_KEY]
    record = store.get(session_id)
    if not record or record.deleted:
        return web.json_response({"error": "session_not_found"}, status=404)
    try:
        messages = await client.list_messages(record.opencode_session_id)
        if len(messages) <= 1:
            return web.json_response(
                {"error": "not_enough_history"}, status=422
            )
        busy, _ = await _busy_state(client, record.opencode_session_id)
        if busy:
            return web.json_response(
                {"error": "session_busy", "detail": "Cannot compact while a response is running."},
                status=409,
            )
        before = await _build_snapshot(client, record, messages)
        provider_id, model_id = _model_ref(messages, record)
        if not provider_id or not model_id:
            return web.json_response(
                {"error": "model_unavailable", "detail": "Send one message with a selected model before compacting."},
                status=422,
            )
        await client.summarize_session(
            record.opencode_session_id,
            provider_id=provider_id,
            model_id=model_id,
            auto=False,
            timeout_seconds=180,
        )
        compacted_messages = await client.list_messages(record.opencode_session_id)
        after = await _build_snapshot(client, record, compacted_messages)
        after.update(
            {
                "success": True,
                "session_id": session_id,
                "compact": {
                    "supported": True,
                    "in_progress": False,
                    "eligible": len(compacted_messages) > 1,
                    "reason": None,
                },
            }
        )
        return web.json_response(
            {
                "success": True,
                "session_id": session_id,
                "checkpoint_id": None,
                "before_message_count": len(messages),
                "after_message_count": len(compacted_messages),
                "before": before,
                "after": after,
            }
        )
    except OpenCodeClientError as exc:
        status = 404 if exc.status in {404, 410} else 502
        return web.json_response(
            {"error": "opencode_error", "detail": str(exc)}, status=status
        )


__all__ = ["compact_session_handler", "context_usage_handler"]
