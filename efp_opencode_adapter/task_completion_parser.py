from __future__ import annotations

import json
import re
from typing import Any

STATUS = {"success", "error", "blocked"}


def _extract_json(text: str) -> dict[str, Any] | None:
    t = text.strip()
    if t.startswith("```"):
        m = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", t)
        if m:
            t = m.group(1)
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start : i + 1])
                        return obj if isinstance(obj, dict) else None
                    except Exception:
                        break
        start = text.find("{", start + 1)
    return None


def _find_superseded(value: Any) -> str | None:
    if isinstance(value, dict):
        if value.get("error_code") == "superseded_by_new_head_sha":
            return "superseded_by_new_head_sha"
        for v in value.values():
            r = _find_superseded(v)
            if r:
                return r
    if isinstance(value, list):
        for v in value:
            r = _find_superseded(v)
            if r:
                return r
    return None


def _with_common_defaults(payload: dict[str, Any]) -> dict[str, Any]:
    payload.setdefault("summary", "")
    payload.setdefault("artifacts", [])
    payload.setdefault("blockers", [])
    payload.setdefault("next_recommendation", None)
    payload.setdefault("audit_trace", [])
    payload.setdefault("external_actions", [])
    return payload


def _extract_delegation_result(payload: dict[str, Any], status: str) -> dict[str, Any]:
    explicit = payload.get("delegation_result")
    if not isinstance(explicit, dict):
        output_payload = payload.get("output_payload")
        if isinstance(output_payload, dict):
            explicit = output_payload.get("delegation_result")
    if isinstance(explicit, dict):
        result = dict(explicit)
    else:
        result = {
            "status": status,
            "summary": payload.get("summary", ""),
            "artifacts": payload.get("artifacts", []),
            "blockers": payload.get("blockers", []),
            "next_recommendation": payload.get("next_recommendation"),
            "audit_trace": payload.get("audit_trace", []),
        }
    result.setdefault("status", status)
    result.setdefault("summary", payload.get("summary", ""))
    result.setdefault("artifacts", payload.get("artifacts", []))
    result.setdefault("blockers", payload.get("blockers", []))
    result.setdefault("next_recommendation", payload.get("next_recommendation"))
    result.setdefault("audit_trace", payload.get("audit_trace", []))
    return result


def parse_task_completion(text: str, *, task_type: str, input_payload: dict[str, Any] | None = None, metadata: dict[str, Any] | None = None) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    raw = text or ""
    parsed = _extract_json(raw)
    error = None
    if parsed is None:
        low = raw.lower()
        status = "success"
        if any(k in low for k in ["permission denied", "permission request", "approval required", "waiting for permission", "cannot proceed due to permission"]):
            status = "blocked"
        elif any(k in low for k in ["blocked", "missing required"]):
            status = "blocked"
        elif any(k in low for k in ["cannot proceed", "failed", "error"]):
            status = "error"
        payload = _with_common_defaults({"summary": raw.strip(), "raw_text": raw})
    else:
        payload = _with_common_defaults(dict(parsed))
        status = str(payload.get("status", "success"))
        if status not in STATUS:
            payload["raw_status"] = status
            status = "success"

    superseded = _find_superseded(parsed if parsed is not None else payload)
    if superseded:
        payload["error_code"] = superseded
        status = "error"

    if task_type in {"github_review_task", "github_pr_review"}:
        payload.setdefault("recommendation", "comment")
        payload.setdefault("review_comments", [])
        payload.setdefault("error_code", None)

    if task_type == "delegation_task":
        result = _extract_delegation_result(payload, status)
        payload = {
            "summary": result.get("summary", ""),
            "artifacts": result.get("artifacts", []),
            "blockers": result.get("blockers", []),
            "next_recommendation": result.get("next_recommendation"),
            "audit_trace": result.get("audit_trace", []),
            "delegation_result": result,
        }

    return status, payload, error
