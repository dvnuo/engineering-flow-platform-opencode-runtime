from __future__ import annotations

from datetime import UTC, datetime
import re
from typing import Any


def message_role(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    info = message.get("info")
    if isinstance(info, dict) and isinstance(info.get("role"), str):
        return info["role"]
    if isinstance(message.get("role"), str):
        return message["role"]
    nested = message.get("message")
    if isinstance(nested, dict):
        return message_role(nested)
    return ""


def message_id(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    info = message.get("info")
    if isinstance(info, dict) and info.get("id"):
        return str(info["id"])
    for key in ("id", "message_id"):
        if message.get(key):
            return str(message[key])
    nested = message.get("message")
    if isinstance(nested, dict):
        return message_id(nested)
    return ""


def extract_visible_text_from_parts(parts: Any, *, include_synthetic: bool = False) -> str:
    if not isinstance(parts, list):
        return ""
    out: list[str] = []
    for part in parts:
        if isinstance(part, dict):
            if part.get("ignored") is True:
                continue
            if part.get("synthetic") is True and not include_synthetic:
                continue
            if part.get("type") == "text" and isinstance(part.get("text"), str) and part.get("text").strip():
                out.append(part["text"])
        elif isinstance(part, str):
            out.append(part)
    return "\n".join(out).strip()


def extract_reasoning_texts_from_parts(parts: Any) -> list[str]:
    if not isinstance(parts, list):
        return []
    out: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "reasoning" and isinstance(part.get("text"), str) and part.get("text").strip():
            out.append(part["text"])
    return out


def message_to_visible_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if not isinstance(message, dict):
        return ""
    nested = message.get("message")
    for parts in (message.get("parts"), nested.get("parts") if isinstance(nested, dict) else None):
        if isinstance(parts, list):
            return extract_visible_text_from_parts(parts)

    for key in ("content", "text", "response"):
        value = message.get(key)
        if isinstance(value, str) and value:
            return value
    if isinstance(nested, dict):
        for key in ("content", "text", "response"):
            value = nested.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _as_iso(value: Any) -> str:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1_000_000_000_000 else value
        return datetime.fromtimestamp(seconds, UTC).isoformat()
    return ""


def timestamp_from_message(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    for key in ("timestamp", "created_at", "updated_at"):
        out = _as_iso(message.get(key))
        if out:
            return out

    def _from_info_time(obj: Any) -> str:
        if not isinstance(obj, dict):
            return ""
        info = obj.get("info")
        if not isinstance(info, dict):
            return ""
        time = info.get("time")
        if not isinstance(time, dict):
            return ""
        for key in ("created", "completed", "updated"):
            out = _as_iso(time.get(key))
            if out:
                return out
        return ""

    out = _from_info_time(message)
    if out:
        return out
    nested = message.get("message")
    return _from_info_time(nested)


def opencode_session_id_from_message(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    for key in ("session_id", "sessionID"):
        if message.get(key):
            return str(message[key])
    info = message.get("info")
    if isinstance(info, dict):
        for key in ("session_id", "sessionID"):
            if info.get(key):
                return str(info[key])
    nested = message.get("message")
    if isinstance(nested, dict):
        return opencode_session_id_from_message(nested)
    return ""


def _iter_messages(payload: Any):
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield item
                nested = item.get("message")
                if isinstance(nested, dict):
                    yield nested
    elif isinstance(payload, dict):
        yield payload
        nested = payload.get("message")
        if isinstance(nested, dict):
            yield nested
        for key in ("messages", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                for msg in _iter_messages(value):
                    yield msg


def extract_last_assistant_visible_text(payload: Any) -> str:
    messages = [m for m in _iter_messages(payload) if message_role(m).lower() == "assistant"]
    for msg in reversed(messages):
        text = message_to_visible_text(msg)
        if text:
            return text
    return ""

_PROGRESS_PATTERNS = [
    re.compile(r"^\s*i am fetching\b.*", re.I),
    re.compile(r"^\s*i'?m fetching\b.*", re.I),
    re.compile(r"^\s*i am retrieving\b.*", re.I),
    re.compile(r"^\s*i am reading\b.*", re.I),
    re.compile(r"^\s*let me fetch\b.*", re.I),
    re.compile(r"^\s*let me retrieve\b.*", re.I),
    re.compile(r"^\s*i will summarize\b.*\bonce i have\b.*", re.I),
]


def is_progress_only_assistant_text(text: str) -> bool:
    normalized = (text or "").strip()
    if not normalized:
        return False
    return any(p.match(normalized) for p in _PROGRESS_PATTERNS)


def summarize_message_runtime_state(message: Any) -> dict[str, Any]:
    nested = message.get("message") if isinstance(message, dict) and isinstance(message.get("message"), dict) else {}
    msg = message if isinstance(message, dict) else {}
    role = message_role(msg).lower()
    text = message_to_visible_text(msg)
    parts = msg.get("parts") if isinstance(msg.get("parts"), list) else (nested.get("parts") if isinstance(nested.get("parts"), list) else [])
    finish_reason = ""
    for source in (msg, nested, msg.get("info") if isinstance(msg.get("info"), dict) else {}, nested.get("info") if isinstance(nested.get("info"), dict) else {}):
        for key in ("finish_reason", "finish", "stop_reason", "reason"):
            value = source.get(key) if isinstance(source, dict) else None
            if isinstance(value, str) and value.strip():
                finish_reason = value.strip().lower()
                break
        if finish_reason:
            break
    has_pending_tool = False
    has_pending_permission = False
    has_tool_error = False
    error_summary = ""
    pending_states = {"", "pending", "running", "started", "queued", "created", "requested", "open"}
    done_states = {"completed", "complete", "done", "success", "resolved"}
    error_states = {"failed", "error", "rejected", "denied"}
    for part in parts if isinstance(parts, list) else []:
        if not isinstance(part, dict):
            continue
        ptype = str(part.get("type") or "").lower()
        status = str(part.get("status") or part.get("state") or "").lower()
        is_permission = "permission" in ptype
        is_tool = ptype in {"tool", "tool_call", "tool-call", "function_call"} or "tool" in ptype
        if not (is_tool or is_permission):
            continue
        if status in error_states:
            has_tool_error = True
            if not error_summary:
                error_summary = str(part.get("error") or part.get("message") or status)
            continue
        if status in done_states:
            continue
        if is_permission and status in pending_states:
            has_pending_permission = True
            continue
        has_output = bool(part.get("result") or part.get("output"))
        if status in pending_states or (is_tool and not has_output):
            has_pending_tool = True
    progress_only = is_progress_only_assistant_text(text)
    explicit_terminal = finish_reason in {"stop", "end_turn", "done", "complete", "completed", "length"}
    terminal = (
        role == "assistant"
        and bool(text)
        and not has_pending_tool
        and not has_pending_permission
        and not has_tool_error
        and (not progress_only or (explicit_terminal and not (has_pending_tool or has_pending_permission)))
        and (explicit_terminal or not progress_only)
    )
    return {
        "message_id": message_id(msg),
        "role": role,
        "text": text,
        "has_visible_text": bool(text),
        "has_pending_tool": has_pending_tool,
        "has_tool_error": has_tool_error,
        "has_pending_permission": has_pending_permission,
        "finish_reason": finish_reason,
        "progress_only": progress_only,
        "terminal": terminal,
        "error_summary": error_summary,
    }


def extract_terminal_assistant_visible_text(payload: Any, *, exclude_message_ids: set[str] | None = None) -> str:
    exclude = exclude_message_ids or set()
    messages = [m for m in _iter_messages(payload) if message_role(m).lower() == "assistant"]
    for msg in reversed(messages):
        state = summarize_message_runtime_state(msg)
        if state["message_id"] and state["message_id"] in exclude:
            continue
        if state["terminal"]:
            return state["text"]
    return ""


def find_latest_assistant_completion(payload: Any, *, exclude_message_ids: set[str] | None = None) -> dict[str, Any]:
    exclude = exclude_message_ids or set()
    states = []
    for msg in _iter_messages(payload):
        state = summarize_message_runtime_state(msg)
        if state["role"] != "assistant":
            continue
        if state["message_id"] and state["message_id"] in exclude:
            continue
        states.append(state)
    for state in reversed(states):
        if state["terminal"]:
            return {"text": state["text"], "message_id": state["message_id"], "completion_state": "completed", "reason": "terminal_assistant_message", "diagnostics": state}
    for state in reversed(states):
        if state["has_pending_permission"]:
            return {"text": "", "message_id": state["message_id"], "completion_state": "blocked", "reason": "pending_permission", "diagnostics": state}
    for state in reversed(states):
        if state["has_tool_error"]:
            return {"text": "", "message_id": state["message_id"], "completion_state": "error", "reason": "tool_error", "diagnostics": state}
    for state in reversed(states):
        if state["has_pending_tool"] or state["progress_only"]:
            return {"text": "", "message_id": state["message_id"], "completion_state": "pending", "reason": "assistant_in_progress", "diagnostics": state}
    return {"text": "", "message_id": "", "completion_state": "incomplete", "reason": "no_terminal_assistant_message", "diagnostics": {"states_seen": len(states)}}


def to_efp_message(message: dict[str, Any], index: int | None = None) -> dict[str, Any]:
    info = message.get("info") if isinstance(message.get("info"), dict) else {}
    parts = message.get("parts") if isinstance(message.get("parts"), list) else []
    if not parts and isinstance(message.get("message"), dict) and isinstance(message["message"].get("parts"), list):
        parts = message["message"].get("parts")
    summary: dict[str, int] = {}
    for part in parts:
        if isinstance(part, dict):
            ptype = str(part.get("type") or "unknown")
            summary[ptype] = summary.get(ptype, 0) + 1

    out_id = message_id(message) or str(index or "")
    out = {
        "id": out_id,
        "role": str(message_role(message) or "unknown"),
        "content": message_to_visible_text(message),
        "timestamp": timestamp_from_message(message),
        "metadata": {
            "opencode_message_id": out_id,
            "opencode_session_id": opencode_session_id_from_message(message),
            "parts_summary": summary,
        },
    }
    return out
