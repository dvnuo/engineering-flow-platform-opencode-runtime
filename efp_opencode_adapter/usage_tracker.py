from __future__ import annotations

import json
from typing import Any
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _extract_usage(payload: Any) -> dict:
    if isinstance(payload, list):
        for item in reversed(payload):
            usage = _extract_usage(item)
            if usage:
                return usage
        return {}

    if not isinstance(payload, dict):
        return {}

    if isinstance(payload.get("usage"), dict):
        return payload["usage"]

    for key in ("message", "data"):
        usage = _extract_usage(payload.get(key))
        if usage:
            return usage

    return {}


def _extract_text_field(payload: Any, key: str) -> str | None:
    if isinstance(payload, list):
        for item in reversed(payload):
            value = _extract_text_field(item, key)
            if value:
                return value
        return None

    if not isinstance(payload, dict):
        return None

    value = payload.get(key)
    if isinstance(value, str) and value:
        return value

    for nested_key in ("message", "data"):
        value = _extract_text_field(payload.get(nested_key), key)
        if value:
            return value

    return None


def _safe_int(value: Any, default: int = 0) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return default
        return float(value)
    except (TypeError, ValueError):
        return default


class UsageTracker:
    def __init__(self, usage_file: Path):
        self.usage_file = usage_file

    def record_chat(self, *, session_id: str, request_id: str, model: str | None, provider: str | None, response_payload: Any, input_text: str, output_text: str) -> dict:
        usage = _extract_usage(response_payload or {})
        input_tokens = _safe_int(usage.get("input_tokens") or usage.get("prompt_tokens"))
        output_tokens = _safe_int(usage.get("output_tokens") or usage.get("completion_tokens"))
        cost = _safe_float(usage.get("cost") or usage.get("total_cost"))

        rec = {
            "timestamp": datetime.now(UTC).isoformat(),
            "type": "chat",
            "session_id": session_id,
            "request_id": request_id,
            "model": model or _extract_text_field(response_payload, "model") or "unknown",
            "provider": provider or _extract_text_field(response_payload, "provider") or "unknown",
            "requests": 1,
            "messages": 2,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost": cost,
        }
        self.usage_file.parent.mkdir(parents=True, exist_ok=True)
        with self.usage_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def summarize(self, *, days: int = 30) -> dict:
        days = min(365, max(1, int(days)))
        cutoff = datetime.now(UTC) - timedelta(days=days)
        rows = []
        if self.usage_file.exists():
            for line in self.usage_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    ts = datetime.fromisoformat(row.get("timestamp", "").replace("Z", "+00:00"))
                    if ts >= cutoff:
                        rows.append(row)
                except Exception:
                    pass

        def agg(items, key):
            out = {}
            for r in items:
                k = r.get(key, "unknown") or "unknown"
                x = out.setdefault(k, {key: k, "total_requests": 0, "total_messages": 0, "total_input_tokens": 0, "total_output_tokens": 0, "total_cost": 0.0})
                x["total_requests"] += int(r.get("requests", 0))
                x["total_messages"] += int(r.get("messages", 0))
                x["total_input_tokens"] += int(r.get("input_tokens", 0))
                x["total_output_tokens"] += int(r.get("output_tokens", 0))
                x["total_cost"] += float(r.get("cost", 0.0))
            return list(out.values())

        g = {
            "total_requests": sum(int(r.get("requests", 0)) for r in rows),
            "total_messages": sum(int(r.get("messages", 0)) for r in rows),
            "total_input_tokens": sum(int(r.get("input_tokens", 0)) for r in rows),
            "total_output_tokens": sum(int(r.get("output_tokens", 0)) for r in rows),
            "total_cost": sum(float(r.get("cost", 0.0)) for r in rows),
        }
        return {"period_days": days, "global": g, "by_model": agg(rows, "model"), "by_provider": agg(rows, "provider")}
