from __future__ import annotations

from typing import Any, Mapping


_DIRECT_RESPONSES = {"once", "always", "reject"}
_DECISION_MAP = {
    "allow_once": ("once", False),
    "allow_always": ("always", True),
    "deny": ("reject", False),
}


def map_permission_response(body: Mapping[str, Any]) -> dict[str, Any]:
    response = body.get("response")
    if isinstance(response, str) and response in _DIRECT_RESPONSES:
        out: dict[str, Any] = {"response": response}
        if "remember" in body:
            out["remember"] = bool(body.get("remember"))
        return out
    if isinstance(response, str) and response:
        raise ValueError("invalid_response")

    decision = str(body.get("decision") or "").strip().lower()
    if decision not in _DECISION_MAP:
        raise ValueError("invalid_decision")
    mapped_response, default_remember = _DECISION_MAP[decision]
    return {"response": mapped_response, "remember": default_remember}
