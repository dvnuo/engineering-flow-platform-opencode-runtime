from __future__ import annotations

import copy
from typing import Any

MUTATION_TAGS = {"write", "mutation", "update", "delete", "comment", "transition", "assign"}
READ_TAGS = {"read_only", "read"}


def default_permission_baseline() -> dict[str, Any]:
    return {
        "*": "ask",
        "read": "allow",
        "glob": "allow",
        "grep": "allow",
        "edit": "ask",
        "write": "ask",
        "bash": {
            "*": "ask",
            "git status*": "allow",
            "git diff*": "allow",
            "git log*": "allow",
            "rm *": "deny",
            "sudo *": "deny",
            "git push *": "deny",
            "curl *|*bash*": "deny",
        },
        "external_directory": "deny",
        "webfetch": "ask",
        "websearch": "ask",
        "skill": {"*": "deny"},
    }


def _as_set(value: Any) -> set[str]:
    if isinstance(value, list):
        return {str(v) for v in value}
    return set()


def build_permission(config: dict, skills_index: dict | None = None, tools_index: dict | None = None) -> dict:
    permission = copy.deepcopy(default_permission_baseline())
    config = config if isinstance(config, dict) else {}
    allowed_ids = _as_set(config.get("allowed_capability_ids"))
    allowed_actions = _as_set(config.get("allowed_actions")) | _as_set(config.get("allowed_adapter_actions"))
    denied_actions = _as_set(config.get("denied_actions"))
    denied_types = _as_set(config.get("denied_capability_types"))

    derived = config.get("derived_runtime_rules") if isinstance(config.get("derived_runtime_rules"), dict) else {}
    policy_ctx = config.get("policy_context") if isinstance(config.get("policy_context"), dict) else {}
    auto_allow = bool(
        derived.get("auto_allow_adapter_actions")
        or derived.get("allow_auto_run")
        or derived.get("auto_run_adapter_actions")
        or policy_ctx.get("auto_allow_adapter_actions")
        or policy_ctx.get("allow_auto_run")
        or policy_ctx.get("auto_run_adapter_actions")
    )

    skills = (skills_index or {}).get("skills", []) if isinstance(skills_index, dict) else []
    known_skills = {str(item.get("opencode_name")) for item in skills if isinstance(item, dict) and item.get("opencode_name")}
    for name in known_skills:
        if {name, f"skill:{name}", f"opencode.skill.{name}"} & allowed_ids:
            permission["skill"][name] = "allow"

    tools = (tools_index or {}).get("tools", []) if isinstance(tools_index, dict) else []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        cap_id = str(tool.get("capability_id") or tool.get("tool_id") or tool.get("action_id") or "")
        name = str(tool.get("opencode_name") or tool.get("name") or "")
        if not name:
            continue
        tags = {str(x).lower() for x in (tool.get("policy_tags") or []) if isinstance(x, str)}
        is_allowed = cap_id in allowed_ids or name in allowed_actions or cap_id in allowed_actions
        if not is_allowed:
            continue
        decision = "ask"
        if tags & READ_TAGS:
            decision = "allow"
        if tags & MUTATION_TAGS:
            decision = "allow" if auto_allow else "ask"
        if cap_id in denied_actions or name in denied_actions or tool.get("type") in denied_types:
            decision = "deny"
        permission[name] = decision

    return permission
