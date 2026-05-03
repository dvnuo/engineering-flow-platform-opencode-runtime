from __future__ import annotations

import copy
from typing import Any

MUTATION_TAGS = {"write", "mutation", "update", "delete", "comment", "transition", "assign", "external_writeback"}
READ_TAGS = {"read_only", "read"}
UNSAFE_TAGS = {"unsafe", "dangerous", "destructive", "credential_exfiltration"}
RESERVED_PERMISSION_KEYS = {"*", "read", "glob", "grep", "edit", "write", "bash", "external_directory", "webfetch", "websearch", "skill", "todowrite", "question"}
KNOWN_EXTERNAL_SYSTEMS = {"github", "jira", "confluence", "gitlab", "bitbucket", "slack", "linear"}
BUILTIN_PERMISSION_ALIASES = {
    "read": {"read", "opencode.builtin.read"},
    "glob": {"glob", "opencode.builtin.glob"},
    "grep": {"grep", "opencode.builtin.grep"},
    "edit": {"edit", "opencode.builtin.edit"},
    "write": {"write", "opencode.builtin.write"},
    "bash": {"bash", "opencode.builtin.bash"},
    "todowrite": {"todowrite", "opencode.builtin.todowrite"},
    "webfetch": {"webfetch", "opencode.builtin.webfetch"},
    "websearch": {"websearch", "opencode.builtin.websearch"},
    "question": {"question", "opencode.builtin.question"},
    "skill": {"skill", "opencode.builtin.skill"},
}


def default_permission_baseline() -> dict[str, Any]:
    return {
        "*": "ask",
        "read": "allow",
        "glob": "allow",
        "grep": "allow",
        "edit": "ask",
        "write": "ask",
        "bash": {"*": "ask", "git status*": "allow", "git diff*": "allow", "git log*": "allow", "rm *": "deny", "sudo *": "deny", "git push *": "deny", "curl *|*bash*": "deny"},
        "external_directory": "deny",
        "webfetch": "ask",
        "websearch": "ask",
        "todowrite": "ask",
        "question": "ask",
        "skill": {"*": "deny"},
    }


def _as_set(value: Any) -> set[str]:
    return {str(v) for v in value} if isinstance(value, list) else set()


def _is_opencode_compatible_tool(tool: dict[str, Any]) -> bool:
    compat = tool.get("runtime_compat")
    if compat is None:
        return True
    if isinstance(compat, str):
        values = {compat.lower()}
    elif isinstance(compat, list):
        values = {str(x).lower() for x in compat}
    else:
        return True
    return "opencode" in values


def _tool_external_systems(tool: dict[str, Any], tags: set[str]) -> set[str]:
    systems = set(tags & KNOWN_EXTERNAL_SYSTEMS)
    for key in ("domain", "external_system", "system_type"):
        value = tool.get(key)
        if isinstance(value, str) and value:
            systems.add(value.lower())
    return systems


def _external_system_allowed(tool_systems: set[str], allowed_external_systems: set[str]) -> bool:
    if not allowed_external_systems or not tool_systems:
        return True
    return bool(tool_systems & allowed_external_systems)


def _apply_builtin_denies(permission: dict[str, Any], denied_actions: set[str], denied_types: set[str]) -> None:
    deny_all_tools = "tool" in denied_types
    deny_shell = "shell" in denied_types
    for key, aliases in BUILTIN_PERMISSION_ALIASES.items():
        if not (deny_all_tools or aliases & denied_actions or (key == "bash" and deny_shell)):
            continue
        if key == "bash":
            permission["bash"]["*"] = "deny"
            permission["bash"]["rm *"] = "deny"
            permission["bash"]["sudo *"] = "deny"
            permission["bash"]["git push *"] = "deny"
            permission["bash"]["curl *|*bash*"] = "deny"
        elif key == "skill":
            permission["skill"]["*"] = "deny"
        else:
            permission[key] = "deny"


def _collect_allowed_and_denied(config: dict) -> tuple[set[str], set[str], set[str], set[str], set[str], set[str]]:
    allowed_ids, allowed_actions, allowed_types, denied_actions, denied_types, allowed_external_systems = set(), set(), set(), set(), set(), set()

    def _merge_rule_block(block: Any) -> None:
        nonlocal allowed_ids, allowed_actions, allowed_types, denied_actions, denied_types, allowed_external_systems
        if not isinstance(block, dict):
            return
        allowed_ids |= _as_set(block.get("allowed_capability_ids"))
        allowed_actions |= _as_set(block.get("allowed_actions"))
        allowed_actions |= _as_set(block.get("allowed_adapter_actions"))
        allowed_types |= _as_set(block.get("allowed_capability_types"))
        denied_actions |= _as_set(block.get("denied_actions"))
        denied_types |= _as_set(block.get("denied_capability_types"))
        allowed_external_systems |= {x.lower() for x in _as_set(block.get("allowed_external_systems"))}

    _merge_rule_block(config)
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
        allowed_types |= _as_set(llm_tools.get("allowed_capability_types"))
        allowed_external_systems |= {x.lower() for x in _as_set(llm_tools.get("allowed_external_systems"))}
    _merge_rule_block(config.get("derived_runtime_rules"))
    _merge_rule_block(config.get("policy_context"))
    return allowed_ids, allowed_actions, allowed_types, denied_actions, denied_types, allowed_external_systems


def build_permission(config: dict, skills_index: dict | None = None, tools_index: dict | None = None) -> dict:
    permission = copy.deepcopy(default_permission_baseline())
    config = config if isinstance(config, dict) else {}
    allowed_ids, allowed_actions, allowed_types, denied_actions, denied_types, allowed_external_systems = _collect_allowed_and_denied(config)
    _apply_builtin_denies(permission, denied_actions, denied_types)

    derived = config.get("derived_runtime_rules") if isinstance(config.get("derived_runtime_rules"), dict) else {}
    policy_ctx = config.get("policy_context") if isinstance(config.get("policy_context"), dict) else {}
    auto_allow = bool(derived.get("auto_allow_adapter_actions") or derived.get("allow_auto_run") or derived.get("auto_run_adapter_actions") or policy_ctx.get("auto_allow_adapter_actions") or policy_ctx.get("allow_auto_run") or policy_ctx.get("auto_run_adapter_actions"))

    skills = (skills_index or {}).get("skills", []) if isinstance(skills_index, dict) else []
    known_skills = {str(i.get("opencode_name")) for i in skills if isinstance(i, dict) and i.get("opencode_name")}
    deny_all_skills = "skill" in denied_types or "skill" in denied_actions or "opencode.builtin.skill" in denied_actions
    for name in known_skills:
        aliases = {name, f"skill:{name}", f"opencode.skill.{name}"}
        if deny_all_skills or aliases & denied_actions:
            permission["skill"][name] = "deny"
            continue
        if aliases & (allowed_ids | allowed_actions) or "skill" in allowed_types:
            permission["skill"][name] = "allow"

    tools = (tools_index or {}).get("tools", []) if isinstance(tools_index, dict) else []
    for tool in tools:
        if not isinstance(tool, dict) or not _is_opencode_compatible_tool(tool):
            continue
        cap_id = str(tool.get("capability_id") or tool.get("tool_id") or tool.get("action_id") or "")
        name = str(tool.get("opencode_name") or tool.get("name") or "")
        typ = str(tool.get("type") or "adapter_action")
        if not cap_id or not name or name in RESERVED_PERMISSION_KEYS:
            continue
        tags = {str(x).lower() for x in (tool.get("policy_tags") or [])}
        if cap_id in denied_actions or name in denied_actions or typ in denied_types or bool(tags & UNSAFE_TAGS):
            permission[name] = "deny"
            continue
        allowed = cap_id in allowed_ids or name in allowed_actions or cap_id in allowed_actions or typ in allowed_types
        if not allowed:
            continue
        if not _external_system_allowed(_tool_external_systems(tool, tags), allowed_external_systems):
            permission[name] = "deny"
            continue
        if tags & READ_TAGS:
            permission[name] = "allow"
        elif tags & MUTATION_TAGS:
            permission[name] = "allow" if auto_allow else "ask"
        else:
            permission[name] = "ask"
    return permission
