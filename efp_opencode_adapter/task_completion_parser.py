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


def parse_task_completion(text: str, *, task_type: str, input_payload: dict[str, Any] | None = None, metadata: dict[str, Any] | None = None) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    raw = text or ""
    parsed = _extract_json(raw)
    error = None
    if parsed is None:
        low = raw.lower()
        status = "success"
        if any(k in low for k in ["permission denied", "cannot proceed"]):
            status = "error"
        if any(k in low for k in ["blocked", "missing required"]):
            status = "blocked"
        payload = {"summary": raw.strip(), "raw_text": raw, "artifacts": [], "blockers": [], "next_recommendation": None, "audit_trace": [], "external_actions": []}
    else:
        payload = dict(parsed)
        status = str(payload.get("status", "success"))
        if status not in STATUS:
            payload["raw_status"] = status
            status = "success"
        payload.setdefault("summary", "")
        payload.setdefault("artifacts", [])
    superseded = _find_superseded(parsed if parsed is not None else payload)
    if superseded:
        payload["error_code"] = superseded
    if task_type in {"github_review_task", "github_pr_review"}:
        payload.setdefault("recommendation", "comment")
        payload.setdefault("review_comments", [])
        payload.setdefault("error_code", None)
    if task_type == "delegation_task":
        delegation_result = {
            "status": status,
            "summary": payload.get("summary", ""),
            "artifacts": payload.get("artifacts", []),
            "blockers": payload.get("blockers", []),
            "next_recommendation": payload.get("next_recommendation"),
            "audit_trace": payload.get("audit_trace", []),
        }
        payload = {"summary": payload.get("summary", ""), "delegation_result": delegation_result}
    return status, payload, error
