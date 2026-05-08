from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Any

SENSITIVE_KEYS = [
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access",
    "refresh",
    "access_token",
    "refresh_token",
    "authorization",
    "auth",
    "credential",
    "private_key",
]


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _is_sensitive_key(key: str) -> bool:
    lk = key.lower()
    return any(s in lk for s in SENSITIVE_KEYS)


def _truncate(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[:max_chars] + "…"


def _redact_env_lines(text: str) -> str:
    out = []
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            if _is_sensitive_key(k.strip()):
                out.append(f"{k}=***REDACTED***")
                continue
        out.append(line)
    return "\n".join(out)


def _redact_token_patterns(text: str) -> str:
    patterns = [
        r"gho_[A-Za-z0-9_\-]+",
        r"ghu_[A-Za-z0-9_\-]+",
        r"ghp_[A-Za-z0-9_\-]+",
        r"github_pat_[A-Za-z0-9_\-]+",
        r"sk-[A-Za-z0-9_\-]+",
    ]
    out = text
    for pattern in patterns:
        out = re.sub(pattern, "***REDACTED***", out)
    return out


def _redact_sensitive_text_values(text: str) -> str:
    key_union = "key|api_key|apikey|access|refresh|access_token|refresh_token|token|authorization|password|secret|oauth"
    out = text
    out = re.sub(
        rf"(?i)([\"']?(?:{key_union})[\"']?\s*:\s*[\"'])([^\"']+)([\"'])",
        r"\1***REDACTED***\3",
        out,
    )
    out = re.sub(
        rf"(?i)\b({key_union})\b(\s*[:=]\s*)([^\s,;&}}\]]+)",
        r"\1\2***REDACTED***",
        out,
    )
    return out


def safe_preview(value: Any, max_chars: int = 500) -> Any:
    if isinstance(value, dict):
        return {k: ("***REDACTED***" if _is_sensitive_key(str(k)) else safe_preview(v, max_chars)) for k, v in value.items()}
    if isinstance(value, list):
        return [safe_preview(v, max_chars) for v in value]
    if isinstance(value, str):
        cleaned = _redact_env_lines(value)
        cleaned = re.sub(
            r"(?im)^(\s*authorization\s*:\s*).*$",
            r"\1***REDACTED***",
            cleaned,
        )
        cleaned = _redact_sensitive_text_values(cleaned)
        cleaned = _redact_token_patterns(cleaned)
        return _truncate(cleaned, max_chars)
    return value


def build_thinking_event(event_type: str, *, session_id: str, request_id: str | None = None, task_id: str | None = None, agent_id: str | None = None, opencode_session_id: str | None = None, state: str | None = None, summary: str | None = None, data: dict[str, Any] | None = None) -> dict[str, Any]:
    evt = {
        "type": event_type,
        "event_type": event_type,
        "engine": "opencode",
        "session_id": session_id,
        "request_id": request_id or "",
        "state": state or "running",
        "summary": safe_preview(summary or "", 500),
        "data": safe_preview(data or {"session_id": session_id, "request_id": request_id or ""}, 500),
        "created_at": utc_now_iso(),
        "ts": time.time(),
    }
    if task_id:
        evt["task_id"] = task_id
    if agent_id:
        evt["agent_id"] = agent_id
    if opencode_session_id:
        evt["opencode_session_id"] = opencode_session_id
    return evt


def chat_started_event(**kwargs: Any) -> dict[str, Any]:
    return build_thinking_event("execution.started", state="running", summary="Execution started", **kwargs)


def llm_thinking_event(*, message: str = "OpenCode is thinking", **kwargs: Any) -> dict[str, Any]:
    return build_thinking_event("llm_thinking", state="running", summary="LLM thinking", data={"message": safe_preview(message)}, **kwargs)


def assistant_delta_event(*, text: str, **kwargs: Any) -> dict[str, Any]:
    preview = safe_preview(text, 500)
    return build_thinking_event("assistant_delta", state="running", summary="Assistant delta", data={"delta": preview, "message": preview}, **kwargs)


def chat_complete_event(*, text: str, **kwargs: Any) -> dict[str, Any]:
    return build_thinking_event("complete", state="success", summary=safe_preview(text, 500), data={"message": safe_preview(text, 500)}, **kwargs)


def chat_completed_compat_event(*, text: str, **kwargs: Any) -> dict[str, Any]:
    return build_thinking_event("execution.completed", state="success", summary=safe_preview(text, 500), data={"message": safe_preview(text, 500)}, **kwargs)


def chat_failed_event(*, error: str, **kwargs: Any) -> dict[str, Any]:
    return build_thinking_event("execution.failed", state="failed", summary=safe_preview(error, 500), data={"error": safe_preview(error, 500)}, **kwargs)


def permission_request_event(*, permission_id: str, tool: str, input_preview: str, risk_level: str = "medium", **kwargs: Any) -> dict[str, Any]:
    evt = build_thinking_event("permission_request", state="pending", summary="Permission requested", data={"permission_id": permission_id, "tool": tool, "input_preview": safe_preview(input_preview), "risk_level": risk_level}, **kwargs)
    evt.update({"permission_id": permission_id, "tool": tool, "input_preview": safe_preview(input_preview), "risk_level": risk_level})
    return evt


def task_lifecycle_event(event_type: str, **kwargs: Any) -> dict[str, Any]:
    return build_thinking_event(event_type, **kwargs)
