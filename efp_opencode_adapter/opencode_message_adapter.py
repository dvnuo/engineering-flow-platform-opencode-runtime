from __future__ import annotations

from datetime import UTC, datetime
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
