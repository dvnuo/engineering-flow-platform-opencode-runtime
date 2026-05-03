from __future__ import annotations

import copy
from typing import Any

MUTATION_TAGS = {"write", "mutation", "update", "delete", "comment", "transition", "assign", "external_writeback"}
READ_TAGS = {"read_only", "read"}
UNSAFE_TAGS = {"unsafe", "dangerous", "destructive", "credential_exfiltration"}
RESERVED_PERMISSION_KEYS = {"*", "read", "glob", "grep", "edit", "write", "bash", "external_directory", "webfetch", "websearch", "skill"}


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
    return {str(v) for v in value} if isinstance(value, list) else set()


def _collect_allowed_and_denied(config: dict) -> tuple[set[str], set[str], set[str], set[str], set[str]]:
    allowed_ids = _as_set(config.get("allowed_capability_ids"))
    allowed_actions = _as_set(config.get("allowed_actions")) | _as_set(config.get("allowed_adapter_actions"))
    allowed_types = _as_set(config.get("allowed_capability_types"))
    denied_actions = _as_set(config.get("denied_actions"))
    denied_types = _as_set(config.get("denied_capability_types"))

    llm = config.get("llm") if isinstance(config.get("llm"), dict) else {}
    llm_tools = llm.get("tools")
    if isinstance(llm_tools, list):
        vals = {str(x) for x in llm_tools}
        allowed_ids |= vals
        allowed_actions |= vals
    elif isinstance(llm_tools, dict):
        allow_vals = _as_set(llm_tools.get("allow")) | _as_set(llm_tools.get("allowed")) | _as_set(llm_tools.get("allowed_capability_ids")) | _as_set(llm_tools.get("allowed_actions"))
        deny_vals = _as_set(llm_tools.get("deny")) | _as_set(llm_tools.get("denied")) | _as_set(llm_tools.get("denied_actions"))
        allowed_ids |= allow_vals
        allowed_actions |= allow_vals
        denied_actions |= deny_vals
        denied_types |= _as_set(llm_tools.get("denied_capability_types"))
    return allowed_ids, allowed_actions, allowed_types, denied_actions, denied_types


def build_permission(config: dict, skills_index: dict | None = None, tools_index: dict | None = None) -> dict:
    permission = copy.deepcopy(default_permission_baseline())
    config = config if isinstance(config, dict) else {}
    allowed_ids, allowed_actions, allowed_types, denied_actions, denied_types = _collect_allowed_and_denied(config)

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
    known_skills = {str(i.get("opencode_name")) for i in skills if isinstance(i, dict) and i.get("opencode_name")}
    for name in known_skills:
        aliases = {name, f"skill:{name}", f"opencode.skill.{name}"}
        if aliases & denied_actions:
            permission["skill"][name] = "deny"
        elif aliases & (allowed_ids | allowed_actions):
            permission["skill"][name] = "allow"

    tools = (tools_index or {}).get("tools", []) if isinstance(tools_index, dict) else []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        cap_id = str(tool.get("capability_id") or tool.get("tool_id") or tool.get("action_id") or "")
        name = str(tool.get("name") or tool.get("opencode_name") or "")
        typ = str(tool.get("type") or "adapter_action")
        if not cap_id or not name or name in RESERVED_PERMISSION_KEYS:
            continue
        tags = {str(x).lower() for x in (tool.get("policy_tags") or [])}
        denied = cap_id in denied_actions or name in denied_actions or typ in denied_types or bool(tags & UNSAFE_TAGS)
        if denied:
            permission[name] = "deny"
            continue
        allowed = cap_id in allowed_ids or name in allowed_actions or cap_id in allowed_actions or typ in allowed_types
        if not allowed:
            continue
        if tags & READ_TAGS:
            permission[name] = "allow"
        elif tags & MUTATION_TAGS:
            permission[name] = "allow" if auto_allow else "ask"
        else:
            permission[name] = "ask"
    return permission
