from __future__ import annotations

import copy
from typing import Any

# external_directory is an OpenCode built-in workspace-escape guardrail key,
# not the removed EFP external-tools subsystem.
RESERVED_PERMISSION_KEYS = {"*", "read", "glob", "grep", "edit", "write", "bash", "external_directory", "webfetch", "websearch", "skill", "todowrite", "question"}
SUPPORTED_BUILTIN_TOOL_PERMISSION_KEYS = (
    "read",
    "glob",
    "grep",
    "edit",
    "write",
    "bash",
    "webfetch",
    "websearch",
    "todowrite",
    "question",
    "skill",
)
BUILTIN_PERMISSION_ALIASES = {
    "read": {"read", "read_file", "file_read", "opencode.builtin.read"},
    "glob": {"glob", "file_search", "opencode.builtin.glob"},
    "grep": {"grep", "search", "opencode.builtin.grep"},
    "edit": {"edit", "apply_patch", "opencode.builtin.edit"},
    "write": {"write", "write_file", "file_write", "opencode.builtin.write"},
    "bash": {"bash", "shell", "shell_exec", "opencode.builtin.bash"},
    "todowrite": {"todowrite", "todo_write", "todo", "opencode.builtin.todowrite"},
    "webfetch": {"webfetch", "fetch", "web_fetch", "opencode.builtin.webfetch"},
    "websearch": {"websearch", "web_search", "opencode.builtin.websearch"},
    "question": {"question", "ask_user", "opencode.builtin.question"},
    "skill": {"skill", "skills", "opencode.builtin.skill"},
}
BUILTIN_PERMISSION_ALIAS_TO_KEY = {
    alias: key
    for key, aliases in BUILTIN_PERMISSION_ALIASES.items()
    for alias in aliases
}
TOOL_PERMISSION_ACTIONS = {"allow", "ask", "deny"}


def normalize_permission_mode(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if value in {"profile_policy", "profile-policy", "policy", "restricted"}:
        return "profile_policy"
    return "workspace_full_access"


def profile_policy_permission_baseline() -> dict[str, Any]:
    atlassian_bash = {
        "jira commands*": "allow",
        "jira schema*": "allow",
        "jira version*": "allow",
        "jira auth test*": "allow",
        "jira instance list*": "allow",
        "jira instance get*": "allow",
        "jira resolve-url*": "allow",
        "jira field list*": "allow",
        "jira issue get*": "allow",
        "jira issue search*": "allow",
        "jira issue createmeta*": "allow",
        "jira issue editmeta*": "allow",
        "jira issue map-csv*": "allow",
        "jira issue bulk-validate*": "allow",
        "jira issue bulk-create *--dry-run*": "allow",
        "jira *": "ask",
        "confluence commands*": "allow",
        "confluence schema*": "allow",
        "confluence version*": "allow",
        "confluence auth test*": "allow",
        "confluence instance list*": "allow",
        "confluence instance get*": "allow",
        "confluence resolve-url*": "allow",
        "confluence search*": "allow",
        "confluence cql*": "allow",
        "confluence page get*": "allow",
        "confluence page body*": "allow",
        "confluence space get*": "allow",
        "confluence space list*": "allow",
        "confluence *": "ask",
    }
    java_maven_bash = {
        "java": "allow",
        "java *": "allow",
        "javac": "allow",
        "javac *": "allow",
        "jar": "allow",
        "jar *": "allow",
        "javadoc": "allow",
        "javadoc *": "allow",
        "jshell": "allow",
        "jshell *": "allow",
        "keytool": "allow",
        "keytool *": "allow",
        "jarsigner": "allow",
        "jarsigner *": "allow",
        "jcmd": "allow",
        "jcmd *": "allow",
        "jconsole": "allow",
        "jconsole *": "allow",
        "jdb": "allow",
        "jdb *": "allow",
        "jdeprscan": "allow",
        "jdeprscan *": "allow",
        "jdeps": "allow",
        "jdeps *": "allow",
        "jfr": "allow",
        "jfr *": "allow",
        "jhsdb": "allow",
        "jhsdb *": "allow",
        "jimage": "allow",
        "jimage *": "allow",
        "jinfo": "allow",
        "jinfo *": "allow",
        "jlink": "allow",
        "jlink *": "allow",
        "jmap": "allow",
        "jmap *": "allow",
        "jmod": "allow",
        "jmod *": "allow",
        "jpackage": "allow",
        "jpackage *": "allow",
        "jps": "allow",
        "jps *": "allow",
        "jrunscript": "allow",
        "jrunscript *": "allow",
        "jstack": "allow",
        "jstack *": "allow",
        "jstat": "allow",
        "jstat *": "allow",
        "jstatd": "allow",
        "jstatd *": "allow",
        "serialver": "allow",
        "serialver *": "allow",
        "jdk": "allow",
        "jdk *": "allow",
        "mvn": "allow",
        "mvn *": "allow",
        "mvn-jdk": "allow",
        "mvn-jdk *": "allow",
        "./mvnw": "allow",
        "./mvnw *": "allow",
        "mvnw": "allow",
        "mvnw *": "allow",
        "bash ./mvnw *": "allow",
        "sh ./mvnw *": "allow",
        "chmod +x ./mvnw": "allow",
    }
    bash = {"*": "ask", "git *": "allow", "gh *": "allow", "git status*": "allow", "git diff*": "allow", "git log*": "allow"}
    bash.update(java_maven_bash)
    bash.update(atlassian_bash)
    return {
        "*": "ask", "read": "allow", "glob": "allow", "grep": "allow", "edit": "ask", "write": "ask",
        "bash": bash,
        "external_directory": "allow", "webfetch": "ask", "websearch": "ask", "todowrite": "ask", "question": "ask", "skill": {"*": "allow"},
    }


def workspace_full_access_permission_baseline(*, allow_bash_all: bool = True) -> dict[str, Any]:
    return {
        "*": "allow", "read": "allow", "glob": "allow", "grep": "allow", "edit": "allow", "write": "allow",
        "bash": {"*": "allow"} if allow_bash_all else {"*": "ask"},
        "external_directory": "allow", "webfetch": "allow", "websearch": "allow", "todowrite": "allow", "question": "allow", "skill": {"*": "allow"},
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


def _normalize_builtin_tool_key(value: Any) -> str | None:
    return BUILTIN_PERMISSION_ALIAS_TO_KEY.get(_norm(value))


def _normalize_builtin_tool_list(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {key for item in value if (key := _normalize_builtin_tool_key(item))}


def _normalize_tool_permission_action(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("action")
    action = _norm(value)
    return action if action in TOOL_PERMISSION_ACTIONS else None


def _ensure_nested_permission(permission: dict[str, Any], key: str) -> dict[str, Any]:
    nested = permission.setdefault(key, {})
    if not isinstance(nested, dict):
        nested = {}
        permission[key] = nested
    return nested


def _set_builtin_tool_action(permission: dict[str, Any], key: str, action: str, *, deny_existing_skill_entries: bool = False) -> None:
    if key == "bash":
        bash_permission = _ensure_nested_permission(permission, "bash")
        if action == "deny":
            for pattern in list(bash_permission.keys()):
                bash_permission[pattern] = "deny"
        bash_permission["*"] = action
    elif key == "skill":
        skill_permission = _ensure_nested_permission(permission, "skill")
        if action == "deny" and deny_existing_skill_entries:
            for name in list(skill_permission.keys()):
                skill_permission[name] = "deny"
        skill_permission["*"] = action
    else:
        permission[key] = action


def _apply_builtin_denies(permission: dict[str, Any], denied_actions: set[str], denied_types: set[str]) -> None:
    denied_actions_norm = {_norm(x) for x in denied_actions}
    denied_types_norm = {_norm(x) for x in denied_types}
    deny_all_tools = "tool" in denied_types_norm
    deny_shell = "shell" in denied_types_norm
    for key, aliases in BUILTIN_PERMISSION_ALIASES.items():
        if not (deny_all_tools or aliases & denied_actions_norm or (key == "bash" and deny_shell)):
            continue
        _set_builtin_tool_action(permission, key, "deny")


def _apply_tool_permissions(permission: dict[str, Any], config: dict[str, Any]) -> None:
    tool_permissions = config.get("tool_permissions")
    if not isinstance(tool_permissions, dict):
        return
    for tool_name, raw_action in tool_permissions.items():
        key = _normalize_builtin_tool_key(tool_name)
        action = _normalize_tool_permission_action(raw_action)
        if key and action:
            _set_builtin_tool_action(permission, key, action, deny_existing_skill_entries=(key == "skill" and action == "deny"))


def _apply_enabled_tools(permission: dict[str, Any], config: dict[str, Any]) -> None:
    if "enabled_tools" not in config or config.get("enabled_tools") is None:
        return
    if not isinstance(config.get("enabled_tools"), list):
        return
    enabled = _normalize_builtin_tool_list(config.get("enabled_tools"))
    for key in SUPPORTED_BUILTIN_TOOL_PERMISSION_KEYS:
        if key not in enabled:
            _set_builtin_tool_action(permission, key, "deny", deny_existing_skill_entries=True)


def _apply_disabled_tools(permission: dict[str, Any], config: dict[str, Any]) -> None:
    disabled = _normalize_builtin_tool_list(config.get("disabled_tools"))
    for key in disabled:
        _set_builtin_tool_action(permission, key, "deny", deny_existing_skill_entries=True)


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

    _apply_tool_permissions(permission, config)
    _apply_enabled_tools(permission, config)
    _apply_disabled_tools(permission, config)

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
