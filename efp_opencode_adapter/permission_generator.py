from __future__ import annotations

import copy
from typing import Any

# external_directory is an OpenCode built-in workspace-escape guardrail key,
# not the removed EFP external-tools subsystem.
RESERVED_PERMISSION_KEYS = {"*", "read", "glob", "grep", "edit", "write", "bash", "external_directory", "webfetch", "websearch", "skill", "todowrite", "question"}
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


def normalize_permission_mode(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if value in {"profile_policy", "profile-policy", "policy", "restricted"}:
        return "profile_policy"
    return "workspace_full_access"


def profile_policy_permission_baseline() -> dict[str, Any]:
    return {
        "*": "ask", "read": "allow", "glob": "allow", "grep": "allow", "edit": "ask", "write": "ask",
        "bash": {"*": "ask", "git status*": "allow", "git diff*": "allow", "git log*": "allow", "rm *": "deny", "sudo *": "deny", "git push *": "deny", "curl *|*bash*": "deny"},
        "external_directory": "deny", "webfetch": "ask", "websearch": "ask", "todowrite": "ask", "question": "ask", "skill": {"*": "allow"},
    }


def workspace_full_access_permission_baseline(*, allow_bash_all: bool = True) -> dict[str, Any]:
    return {
        "*": "allow", "read": "allow", "glob": "allow", "grep": "allow", "edit": "allow", "write": "allow",
        "bash": {"*": "allow"} if allow_bash_all else {"*": "ask"},
        "external_directory": "deny", "webfetch": "allow", "websearch": "allow", "todowrite": "allow", "question": "allow", "skill": {"*": "allow"},
    }


def default_permission_baseline(*, permission_mode: str = "workspace_full_access", allow_bash_all: bool = True) -> dict[str, Any]:
    return profile_policy_permission_baseline() if normalize_permission_mode(permission_mode) == "profile_policy" else workspace_full_access_permission_baseline(allow_bash_all=allow_bash_all)


def _as_set(value: Any) -> set[str]:
    return {str(v) for v in value} if isinstance(value, list) else set()


def _as_name_set(value: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(value, str) and value.strip():
        out.add(value.strip())
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            if isinstance(item, str) and item.strip():
                out.add(item.strip())
    return out


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _apply_builtin_denies(permission: dict[str, Any], denied_actions: set[str], denied_types: set[str]) -> None:
    deny_all_tools = "tool" in denied_types
    deny_shell = "shell" in denied_types
    for key, aliases in BUILTIN_PERMISSION_ALIASES.items():
        if not (deny_all_tools or aliases & denied_actions or (key == "bash" and deny_shell)):
            continue
        if key == "bash":
            bash_permission = permission.setdefault("bash", {})
            if not isinstance(bash_permission, dict):
                bash_permission = {}
                permission["bash"] = bash_permission
            for pattern in list(bash_permission.keys()):
                bash_permission[pattern] = "deny"
            bash_permission["*"] = "deny"
        elif key == "skill":
            permission["skill"]["*"] = "deny"
        else:
            permission[key] = "deny"


def _collect_allowed_and_denied(config: dict) -> tuple[set[str], set[str], set[str], set[str], set[str], set[str], set[str], set[str]]:
    allowed_ids, allowed_actions, allowed_types, denied_actions, denied_types, allowed_external_systems, allowed_skill_names, denied_skill_names = set(), set(), set(), set(), set(), set(), set(), set()

    def _merge_rule_block(block: Any) -> None:
        nonlocal allowed_ids, allowed_actions, allowed_types, denied_actions, denied_types, allowed_external_systems, allowed_skill_names, denied_skill_names
        if not isinstance(block, dict):
            return
        allowed_ids |= _as_set(block.get("allowed_capability_ids"))
        allowed_actions |= _as_set(block.get("allowed_actions")) | _as_set(block.get("allowed_adapter_actions"))
        allowed_types |= _as_set(block.get("allowed_capability_types"))
        denied_actions |= _as_set(block.get("denied_actions"))
        denied_types |= _as_set(block.get("denied_capability_types"))
        allowed_external_systems |= {x.lower() for x in _as_set(block.get("allowed_external_systems"))}
        allowed_skill_names |= _as_name_set(block.get("allowed_skills")) | _as_name_set(block.get("skill_set"))
        denied_skill_names |= _as_name_set(block.get("denied_skills"))

    _merge_rule_block(config)
    llm = config.get("llm") if isinstance(config.get("llm"), dict) else {}
    llm_tools = llm.get("tools")
    if isinstance(llm_tools, list):
        vals = {str(x) for x in llm_tools}
        allowed_ids |= vals; allowed_actions |= vals
    elif isinstance(llm_tools, dict):
        allow_vals = _as_set(llm_tools.get("allow")) | _as_set(llm_tools.get("allowed")) | _as_set(llm_tools.get("allowed_capability_ids")) | _as_set(llm_tools.get("allowed_actions"))
        deny_vals = _as_set(llm_tools.get("deny")) | _as_set(llm_tools.get("denied")) | _as_set(llm_tools.get("denied_actions"))
        allowed_ids |= allow_vals; allowed_actions |= allow_vals; denied_actions |= deny_vals
        denied_types |= _as_set(llm_tools.get("denied_capability_types")); allowed_types |= _as_set(llm_tools.get("allowed_capability_types"))
        allowed_external_systems |= {x.lower() for x in _as_set(llm_tools.get("allowed_external_systems"))}
        allowed_skill_names |= _as_name_set(llm_tools.get("allowed_skills")); denied_skill_names |= _as_name_set(llm_tools.get("denied_skills"))
    _merge_rule_block(config.get("derived_runtime_rules")); _merge_rule_block(config.get("policy_context"))
    return allowed_ids, allowed_actions, allowed_types, denied_actions, denied_types, allowed_external_systems, allowed_skill_names, denied_skill_names


def build_permission(config: dict, skills_index: dict | None = None, *, permission_mode: str = "workspace_full_access", allow_bash_all: bool = True) -> dict:
    mode = normalize_permission_mode(permission_mode)
    permission = copy.deepcopy(default_permission_baseline(permission_mode=mode, allow_bash_all=allow_bash_all))
    config = config if isinstance(config, dict) else {}
    allowed_ids, allowed_actions, allowed_types, denied_actions, denied_types, _ignored_external_systems, allowed_skill_names, denied_skill_names = _collect_allowed_and_denied(config)
    if mode == "profile_policy":
        _apply_builtin_denies(permission, denied_actions, denied_types)

    allowed_values = {_norm(x) for x in (allowed_ids | allowed_actions)}
    denied_values = {_norm(x) for x in denied_actions}
    allowed_type_values = {_norm(x) for x in allowed_types}
    denied_type_values = {_norm(x) for x in denied_types}
    allowed_skill_values = {_norm(x) for x in allowed_skill_names}
    denied_skill_values = {_norm(x) for x in denied_skill_names}

    skills = (skills_index or {}).get("skills", []) if isinstance(skills_index, dict) else []
    known_skills = [i for i in skills if isinstance(i, dict) and i.get("opencode_name")]
    permission_skill = permission.setdefault("skill", {})
    if not isinstance(permission_skill, dict):
        permission_skill = {}
        permission["skill"] = permission_skill

    deny_all_skills = "tool" in denied_type_values or "skill" in denied_type_values or "skill" in denied_values or "opencode.builtin.skill" in denied_values or "*" in denied_skill_values
    allow_all_skills = "*" in allowed_skill_values or "skill" in allowed_type_values or "skill" in allowed_values or "opencode.builtin.skill" in allowed_values

    permission_skill["*"] = "deny" if deny_all_skills else "allow"
    for skill in known_skills:
        name = str(skill.get("opencode_name")); efp_name = str(skill.get("efp_name") or "")
        aliases_norm = {_norm(a) for a in {name, efp_name, f"skill:{name}", f"skill:{efp_name}", f"opencode.skill.{name}"} if _norm(a)}
        if deny_all_skills or (aliases_norm & denied_values) or (aliases_norm & denied_skill_values):
            permission_skill[name] = "deny"
        elif allow_all_skills or (aliases_norm & allowed_values) or (aliases_norm & allowed_skill_values):
            permission_skill[name] = "allow"

    return permission


def skill_permission_state(permission: dict[str, Any], skill_name: str) -> str:
    skill = permission.get("skill") if isinstance(permission, dict) else None
    if not isinstance(skill, dict):
        return "allowed"
    value = skill.get(skill_name, skill.get("*", "allow"))
    norm = _norm(value)
    if norm == "deny":
        return "denied"
    if norm == "ask":
        return "ask"
    return "allowed"
