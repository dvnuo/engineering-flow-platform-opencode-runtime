"""Usage rows for the Portal's Usage view and each chat answer's cost line.

OpenCode books tokens per assistant message (``info.tokens``: ``input``,
``output``, ``reasoning`` and ``cache.read`` / ``cache.write``) together with
a ``cost`` that is 0 for Copilot models (models.dev prices a subscription
provider at nothing). A chat run is one assistant message per model step, and
the send response is only the last of them, so ``record_chat`` sums the
run's assistant messages when the caller hands them over and falls back to
whatever usage the response payload itself carries (``usage`` blocks, as the
tests' fake client and older payloads send). Rows keep the cached, cache-write
and reasoning tokens and estimate GitHub AI credits with the published Copilot
price table (``copilot_pricing``) when OpenCode reports no cost. Rows are
appended to ``usage.jsonl``; older rows without the new fields still
summarize.

``input_tokens`` is the whole prompt (cached and cache-write tokens included,
the OpenAI convention the native runtime and the Portal use), which is the
number the pricing tier is judged on.
"""
from __future__ import annotations

import json
import math
from typing import Any, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .copilot_pricing import CREDIT_USD, estimate_usage_credits

UTC = timezone.utc

# The usage row type of the one-shot session title call (session_title.py).
TITLE_TASK_TYPE = "session_title"

_TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens")


def _safe_int(value: Any, default: int = 0) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return default
        numeric = float(value)
        if not math.isfinite(numeric):
            return default
        return int(numeric)
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return default
        numeric = float(value)
        if not math.isfinite(numeric):
            return default
        return numeric
    except (TypeError, ValueError, OverflowError):
        return default


def _mapping(value: Any) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _usage_from_usage_block(raw: Mapping[str, Any]) -> dict:
    """Normalize an OpenAI-style ``usage`` block (what the fake client and the title call send)."""

    input_details = _mapping(raw.get("input_tokens_details") or raw.get("prompt_tokens_details"))
    output_details = _mapping(raw.get("output_tokens_details") or raw.get("completion_tokens_details"))
    cached = _safe_int(raw.get("cached_input_tokens") or input_details.get("cached_tokens") or raw.get("cache_read_tokens"))
    cache_write = _safe_int(raw.get("cache_write_tokens") or raw.get("cache_creation_input_tokens"))
    reasoning = _safe_int(raw.get("reasoning_tokens") or output_details.get("reasoning_tokens"))
    return {
        "input_tokens": _safe_int(raw.get("input_tokens") or raw.get("prompt_tokens")),
        "cached_input_tokens": cached,
        "cache_write_tokens": cache_write,
        "output_tokens": _safe_int(raw.get("output_tokens") or raw.get("completion_tokens")),
        "reasoning_tokens": reasoning,
        "cost": _safe_float(raw.get("cost") or raw.get("total_cost")),
    }


def _usage_from_tokens(tokens: Mapping[str, Any], cost: Any = None) -> dict:
    """Normalize OpenCode's ``tokens`` block; ``input`` there excludes the cache tokens."""

    cache = _mapping(tokens.get("cache"))
    cache_read = _safe_int(cache.get("read") or tokens.get("cache_read_tokens"))
    cache_write = _safe_int(cache.get("write") or tokens.get("cache_write_tokens"))
    fresh_input = _safe_int(tokens.get("input") or tokens.get("input_tokens") or tokens.get("prompt_tokens"))
    return {
        "input_tokens": fresh_input + cache_read + cache_write,
        "cached_input_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "output_tokens": _safe_int(tokens.get("output") or tokens.get("output_tokens") or tokens.get("completion_tokens")),
        "reasoning_tokens": _safe_int(tokens.get("reasoning") or tokens.get("reasoning_tokens")),
        "cost": _safe_float(cost),
    }


def _message_info(message: Mapping[str, Any]) -> dict:
    info = message.get("info")
    return dict(info) if isinstance(info, Mapping) else {}


def _usage_from_message(message: Any) -> dict:
    """One message's (or one payload's) normalized usage, or {} when it books none."""

    if not isinstance(message, Mapping):
        return {}
    if isinstance(message.get("usage"), Mapping):
        return _usage_from_usage_block(message["usage"])
    info = _message_info(message)
    for source in (info, message):
        tokens = source.get("tokens")
        if isinstance(tokens, Mapping) and tokens:
            return _usage_from_tokens(tokens, source.get("cost"))
    nested = message.get("message")
    if isinstance(nested, Mapping):
        return _usage_from_message(nested)
    return {}


def _extract_usage(payload: Any) -> dict:
    """The usage of the latest item in ``payload`` that books any (a message, a list, or a wrapper)."""

    if isinstance(payload, list):
        for item in reversed(payload):
            usage = _extract_usage(item)
            if usage:
                return usage
        return {}
    if not isinstance(payload, Mapping):
        return {}
    direct = _usage_from_message(payload)
    if direct:
        return direct
    for key in ("message", "data"):
        usage = _extract_usage(payload.get(key))
        if usage:
            return usage
    return {}


def _sum_usage(usages: Iterable[Mapping[str, Any]]) -> dict:
    total = {field: 0 for field in _TOKEN_FIELDS}
    total["cost"] = 0.0
    for usage in usages:
        for field in _TOKEN_FIELDS:
            total[field] += _safe_int(usage.get(field))
        total["cost"] += _safe_float(usage.get("cost"))
    return total


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
    # OpenCode's own message shape names the model inside ``info``.
    info = payload.get("info")
    if isinstance(info, dict):
        aliases = {"model": ("modelID", "model_id"), "provider": ("providerID", "provider_id")}.get(key, ())
        for alias in (key, *aliases):
            value = info.get(alias)
            if isinstance(value, str) and value:
                return value

    for nested_key in ("message", "data"):
        value = _extract_text_field(payload.get(nested_key), key)
        if value:
            return value

    return None


class UsageTracker:
    def __init__(self, usage_file: Path):
        self.usage_file = usage_file

    def _write_row(
        self,
        *,
        row_type: str,
        session_id: str,
        request_id: str,
        model: str,
        provider: str,
        messages: int,
        usage: Mapping[str, Any],
        iterations: int | None = None,
        largest_prompt_tokens: int | None = None,
    ) -> dict:
        normalized = _usage_from_usage_block(usage) if not set(_TOKEN_FIELDS) <= set(usage) else {
            **{field: _safe_int(usage.get(field)) for field in _TOKEN_FIELDS},
            "cost": _safe_float(usage.get("cost") or usage.get("total_cost")),
        }
        estimate = estimate_usage_credits(model, normalized, largest_prompt_tokens=largest_prompt_tokens)
        credits = estimate["estimated_credits"]
        reported_cost = normalized["cost"]
        # OpenCode reports 0 for Copilot models; the published price table
        # fills the USD figure the older Usage card already shows.
        cost = reported_cost if reported_cost > 0 else (round(credits * CREDIT_USD, 6) if credits is not None else 0.0)
        rec = {
            "timestamp": datetime.now(UTC).isoformat(),
            "type": row_type,
            "session_id": session_id,
            "request_id": request_id,
            "model": model,
            "provider": provider,
            "requests": 1,
            "messages": messages,
            "input_tokens": normalized["input_tokens"],
            "output_tokens": normalized["output_tokens"],
            "cached_input_tokens": normalized["cached_input_tokens"],
            "cache_write_tokens": normalized["cache_write_tokens"],
            "reasoning_tokens": normalized["reasoning_tokens"],
            "cost": cost,
            "iterations": iterations,
            "cache_hit_ratio": estimate["cache_hit_ratio"],
            "estimated_credits": credits,
            "pricing_tier": estimate["pricing"]["tier"],
            "pricing": estimate["pricing"],
        }
        self.usage_file.parent.mkdir(parents=True, exist_ok=True)
        with self.usage_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def record_chat(
        self,
        *,
        session_id: str,
        request_id: str,
        model: str | None,
        provider: str | None,
        response_payload: Any,
        input_text: str,
        output_text: str,
        run_messages: Iterable[Any] | None = None,
        iterations: int | None = None,
    ) -> dict:
        """One chat run: the sum of ``run_messages`` (the run's assistant messages) when given, else the payload's usage.

        ``iterations`` is the run's assistant step count when the caller knows
        it; otherwise it is the number of run messages that booked tokens.
        The pricing tier is judged on the largest single request of the run.
        """

        step_usages = [usage for usage in (_usage_from_message(message) for message in (run_messages or [])) if usage]
        if step_usages:
            usage: Mapping[str, Any] = _sum_usage(step_usages)
            largest_prompt = max(_safe_int(step.get("input_tokens")) for step in step_usages)
            if iterations is None:
                iterations = len(step_usages)
        else:
            usage = _extract_usage(response_payload or {})
            largest_prompt = None
        return self._write_row(
            row_type="chat",
            session_id=session_id,
            request_id=request_id,
            model=model or _extract_text_field(response_payload, "model") or "unknown",
            provider=provider or _extract_text_field(response_payload, "provider") or "unknown",
            messages=2,
            usage=usage,
            iterations=iterations,
            largest_prompt_tokens=largest_prompt,
        )

    def record_session_title(self, *, session_id: str, model: str | None, provider: str | None, usage: Any) -> dict:
        """The one-shot title call after a session's first turn (session_title.py): one request, no messages.

        ``usage`` is the reply's usage block itself or a payload that carries
        one; a reply without one still counts as a request with zero tokens.
        """
        raw = usage if isinstance(usage, dict) else {}
        return self._write_row(
            row_type=TITLE_TASK_TYPE,
            session_id=session_id,
            request_id="",
            model=model or "unknown",
            provider=provider or "unknown",
            messages=0,
            usage=_extract_usage(raw) or raw,
            iterations=1,
        )

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

        def totals(items) -> dict:
            return {
                "total_requests": sum(_safe_int(r.get("requests")) for r in items),
                "total_messages": sum(_safe_int(r.get("messages")) for r in items),
                "total_input_tokens": sum(_safe_int(r.get("input_tokens")) for r in items),
                "total_output_tokens": sum(_safe_int(r.get("output_tokens")) for r in items),
                "total_cached_input_tokens": sum(_safe_int(r.get("cached_input_tokens")) for r in items),
                "total_cache_write_tokens": sum(_safe_int(r.get("cache_write_tokens")) for r in items),
                "total_reasoning_tokens": sum(_safe_int(r.get("reasoning_tokens")) for r in items),
                "total_cost": sum(_safe_float(r.get("cost")) for r in items),
                # Rows older than the credits estimate count as 0 here; the row
                # count tells how many rows carry an estimate.
                "total_estimated_credits": round(sum(_safe_float(r.get("estimated_credits")) for r in items), 2),
                "estimated_rows": sum(1 for r in items if r.get("estimated_credits") is not None),
            }

        def agg(items, key):
            groups: dict[str, list] = {}
            for r in items:
                k = r.get(key, "unknown") or "unknown"
                if not isinstance(k, str):
                    k = str(k)
                groups.setdefault(k, []).append(r)
            return [{key: k, **totals(group)} for k, group in groups.items()]

        return {"period_days": days, "global": totals(rows), "by_model": agg(rows, "model"), "by_provider": agg(rows, "provider")}
