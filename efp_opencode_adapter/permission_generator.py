from __future__ import annotations

import copy
from typing import Any

MUTATION_TAGS = {"write", "mutation", "update", "delete", "comment", "transition", "assign", "external_writeback", "execute"}
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




def normalize_permission_mode(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if value in {"profile_policy", "profile-policy", "policy", "restricted"}:
        return "profile_policy"
    return "workspace_full_access"


def profile_policy_permission_baseline() -> dict[str, Any]:
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
        "skill": {"*": "allow"},
    }


def workspace_full_access_permission_baseline(*, allow_bash_all: bool = True) -> dict[str, Any]:
    return {
        "*": "allow",
        "read": "allow",
        "glob": "allow",
        "grep": "allow",
        "edit": "allow",
        "write": "allow",
        "bash": {"*": "allow"} if allow_bash_all else {"*": "ask"},
        "external_directory": "deny",
        "webfetch": "allow",
        "websearch": "allow",
        "todowrite": "allow",
        "question": "allow",
        "skill": {"*": "allow"},
    }


def default_permission_baseline(*, permission_mode: str = "workspace_full_access", allow_bash_all: bool = True) -> dict[str, Any]:
    mode = normalize_permission_mode(permission_mode)
    if mode == "profile_policy":
        return profile_policy_permission_baseline()
    return workspace_full_access_permission_baseline(allow_bash_all=allow_bash_all)


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
            elif isinstance(item, dict):
                for key in ("name", "opencode_name", "efp_name"):
                    v = item.get(key)
                    if isinstance(v, str) and v.strip():
                        out.add(v.strip())
    elif isinstance(value, dict):
        out |= {str(k).strip() for k in value.keys() if str(k).strip()}
        for key in ("name", "opencode_name", "efp_name"):
            v = value.get(key)
            if isinstance(v, str) and v.strip():
                out.add(v.strip())
    return out


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




def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _prefixed_aliases(value: Any) -> set[str]:
    raw = str(value or "").strip()
    if not raw:
        return set()
    lower = raw.lower()
    return {raw, lower, f"tool:{raw}", f"tool:{lower}", f"adapter_action:{raw}", f"adapter_action:{lower}"}


def _portal_seed_aliases_for_tool(tool: dict[str, Any], tags: set[str], systems: set[str]) -> set[str]:
    aliases: set[str] = set()
    if "jira" in systems:
        if tags & READ_TAGS:
            aliases.update({
                "adapter:jira:read_issue",
                "adapter:jira:search_issue",
                "adapter:jira:search_issues",
                "read_issue",
                "search_issue",
                "search_issues",
            })
        if "comment" in tags:
            aliases.update({"adapter:jira:add_comment", "add_comment"})
        if "transition" in tags:
            aliases.update({"adapter:jira:transition_issue", "transition_issue"})
        if "assign" in tags:
            aliases.update({"adapter:jira:assign_issue", "assign_issue"})
        if "update" in tags:
            aliases.update({"adapter:jira:update_issue", "update_issue"})
    return {a.lower() for a in aliases if a}


def _tool_permission_aliases(tool: dict[str, Any], cap_id: str, name: str, typ: str, tags: set[str]) -> set[str]:
    aliases: set[str] = set()
    systems = _tool_external_systems(tool, tags)
    for key in ("capability_id", "tool_id", "action_id", "name", "opencode_name", "legacy_name", "native_name", "efp_name"):
        aliases |= _prefixed_aliases(tool.get(key))
    aliases |= _prefixed_aliases(cap_id)
    aliases |= _prefixed_aliases(name)
    aliases |= _prefixed_aliases(typ)
    aliases.update(_portal_seed_aliases_for_tool(tool, tags, systems))
    return {_norm(a) for a in aliases if _norm(a)}
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
            bash_permission = permission.setdefault("bash", {})
            if not isinstance(bash_permission, dict):
                bash_permission = {}
                permission["bash"] = bash_permission
            for pattern in list(bash_permission.keys()):
                bash_permission[pattern] = "deny"
            bash_permission["*"] = "deny"
            bash_permission["git status*"] = "deny"
            bash_permission["git diff*"] = "deny"
            bash_permission["git log*"] = "deny"
            bash_permission["rm *"] = "deny"
            bash_permission["sudo *"] = "deny"
            bash_permission["git push *"] = "deny"
            bash_permission["curl *|*bash*"] = "deny"
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
        allowed_actions |= _as_set(block.get("allowed_actions"))
        allowed_actions |= _as_set(block.get("allowed_adapter_actions"))
        allowed_types |= _as_set(block.get("allowed_capability_types"))
        denied_actions |= _as_set(block.get("denied_actions"))
        denied_types |= _as_set(block.get("denied_capability_types"))
        allowed_external_systems |= {x.lower() for x in _as_set(block.get("allowed_external_systems"))}
        allowed_skill_names |= _as_name_set(block.get("allowed_skills"))
        allowed_skill_names |= _as_name_set(block.get("skill_set"))
        allowed_skill_names |= _as_name_set(block.get("allowed_skill_names"))
        denied_skill_names |= _as_name_set(block.get("denied_skills"))
        denied_skill_names |= _as_name_set(block.get("denied_skill_names"))
        for nested in (block.get("capability_profile"), block.get("runtime_profile"), block.get("policy_profile")):
            if isinstance(nested, dict):
                allowed_skill_names |= _as_name_set(nested.get("skill_set"))
                allowed_skill_names |= _as_name_set(nested.get("skills"))
                allowed_skill_names |= _as_name_set(nested.get("allowed_skills"))
                denied_skill_names |= _as_name_set(nested.get("denied_skills"))

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
        allowed_skill_names |= _as_name_set(llm_tools.get("allowed_skills"))
        denied_skill_names |= _as_name_set(llm_tools.get("denied_skills"))
    _merge_rule_block(config.get("derived_runtime_rules"))
    _merge_rule_block(config.get("policy_context"))
    return allowed_ids, allowed_actions, allowed_types, denied_actions, denied_types, allowed_external_systems, allowed_skill_names, denied_skill_names


def _llm_tools_configured(config: dict[str, Any]) -> bool:
    llm = config.get("llm") if isinstance(config.get("llm"), dict) else {}
    return "tools" in llm


def build_permission(config: dict, skills_index: dict | None = None, tools_index: dict | None = None, *, permission_mode: str = "workspace_full_access", allow_bash_all: bool = True) -> dict:
    mode = normalize_permission_mode(permission_mode)
    permission = copy.deepcopy(default_permission_baseline(permission_mode=mode, allow_bash_all=allow_bash_all))
    config = config if isinstance(config, dict) else {}
    llm_tools_configured = _llm_tools_configured(config)
    allowed_ids, allowed_actions, allowed_types, denied_actions, denied_types, allowed_external_systems, allowed_skill_names, denied_skill_names = _collect_allowed_and_denied(config)
    if mode == "profile_policy":
        _apply_builtin_denies(permission, denied_actions, denied_types)

    derived = config.get("derived_runtime_rules") if isinstance(config.get("derived_runtime_rules"), dict) else {}
    policy_ctx = config.get("policy_context") if isinstance(config.get("policy_context"), dict) else {}
    auto_allow = bool(derived.get("auto_allow_adapter_actions") or derived.get("allow_auto_run") or derived.get("auto_run_adapter_actions") or policy_ctx.get("auto_allow_adapter_actions") or policy_ctx.get("allow_auto_run") or policy_ctx.get("auto_run_adapter_actions"))

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

    deny_all_skills = (
        "tool" in denied_type_values
        or "skill" in denied_type_values
        or "skill" in denied_values
        or "opencode.builtin.skill" in denied_values
        or "*" in denied_skill_values
    )

    allow_all_skills = (
        "*" in allowed_skill_values
        or "skill" in allowed_type_values
        or "skill" in allowed_values
        or "opencode.builtin.skill" in allowed_values
    )

    if deny_all_skills:
        permission_skill["*"] = "deny"
    else:
        permission_skill["*"] = "allow"

    for skill in known_skills:
        name = str(skill.get("opencode_name"))
        efp_name = str(skill.get("efp_name") or "")

        aliases = {
            name,
            efp_name,
            f"skill:{name}",
            f"skill:{efp_name}",
            f"opencode.skill.{name}",
        }
        aliases_norm = {_norm(a) for a in aliases if _norm(a)}

        if deny_all_skills or (aliases_norm & denied_values) or (aliases_norm & denied_skill_values):
            permission_skill[name] = "deny"
            continue

        if (
            allow_all_skills
            or (aliases_norm & allowed_values)
            or (aliases_norm & allowed_skill_values)
            or permission_skill.get("*") == "allow"
        ):
            permission_skill[name] = "allow"

    allow_all_generated_tools = "*" in allowed_values or not llm_tools_configured

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
        typ_norm = _norm(typ)
        aliases = _tool_permission_aliases(tool, cap_id, name, typ, tags)
        if (aliases & denied_values) or typ_norm in denied_type_values or bool(tags & UNSAFE_TAGS):
            permission[name] = "deny"
            continue
        allowed = allow_all_generated_tools or bool(aliases & allowed_values) or typ_norm in allowed_type_values
        if not allowed:
            continue
        if not _external_system_allowed(_tool_external_systems(tool, tags), allowed_external_systems):
            permission[name] = "deny"
            continue
        permission_default = _norm(tool.get("permission_default"))
        explicit_allow = bool(aliases & allowed_values) or typ_norm in allowed_type_values or cap_id in allowed_ids
        if tags & READ_TAGS:
            permission[name] = "allow"
        elif tags & MUTATION_TAGS:
            if permission_default == "deny":
                permission[name] = "deny"
            elif permission_default == "ask":
                permission[name] = "allow" if explicit_allow else "ask"
            else:
                permission[name] = "allow" if mode == "workspace_full_access" else "ask"
        else:
            if permission_default == "deny":
                permission[name] = "deny"
            elif permission_default == "ask":
                permission[name] = "allow" if explicit_allow else "ask"
            else:
                permission[name] = "allow" if mode == "workspace_full_access" else "ask"

    if allow_bash_all:
        permission["bash"] = {"*": "allow"}
    return permission


def _normalize_permission_state(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    mapping = {"allow": "allowed", "allowed": "allowed", "ask": "ask", "prompt": "ask", "deny": "denied", "denied": "denied"}
    return mapping.get(value, "unknown")


def _skill_aliases(skill_name: str) -> list[str]:
    name = str(skill_name or "").strip()
    if not name:
        return []
    hyphen = name.replace("_", "-")
    underscore = name.replace("-", "_")
    aliases = [
        name, hyphen, underscore,
        f"skill:{name}", f"skill:{hyphen}", f"skill:{underscore}",
        f"opencode.skill.{name}", f"opencode.skill.{hyphen}", f"opencode.skill.{underscore}",
    ]
    return list(dict.fromkeys(aliases))


def skill_permission_state(permission: dict[str, Any], skill_name: str) -> str:
    if not isinstance(permission, dict):
        return "unknown"
    skill_perm = permission.get("skill")
    if isinstance(skill_perm, str):
        return _normalize_permission_state(skill_perm)
    if not isinstance(skill_perm, dict):
        return "unknown"
    for alias in _skill_aliases(skill_name):
        if alias in skill_perm:
            return _normalize_permission_state(skill_perm.get(alias))
    if "*" in skill_perm:
        return _normalize_permission_state(skill_perm.get("*"))
    return "unknown"
