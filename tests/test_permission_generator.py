import inspect
import pytest
from efp_opencode_adapter.permission_generator import build_permission, default_permission_baseline, skill_permission_state


def test_build_permission_signature_has_no_tools_index():
    sig = inspect.signature(build_permission)
    assert "tools_index" not in sig.parameters


def test_old_tools_index_argument_is_rejected():
    with pytest.raises(TypeError):
        build_permission({}, None, {"tools": [{"name": "old"}]})
    with pytest.raises(TypeError):
        build_permission({}, skills_index=None, tools_index={"tools": []})


def test_baseline_safety():
    ws = default_permission_baseline(permission_mode="workspace_full_access", allow_bash_all=True)
    assert ws["*"] == "allow"
    for k in ["read", "glob", "grep", "edit", "write", "webfetch", "websearch", "todowrite", "question"]:
        assert ws[k] == "allow"
    assert ws["bash"]["*"] == "allow"
    assert ws["skill"]["*"] == "allow"
    assert ws["external_directory"] == "allow"

    pp = default_permission_baseline(permission_mode="profile_policy", allow_bash_all=False)
    assert pp["*"] == "ask"
    assert pp["edit"] == "ask" and pp["write"] == "ask"
    assert pp["bash"]["*"] == "ask"
    assert pp["bash"]["git *"] == "allow"
    assert pp["bash"]["gh *"] == "allow"
    for pattern in [
        "mvn *",
        "mvn-jdk *",
        "jdk *",
        "java *",
        "jcmd *",
        "jdeps *",
        "jlink *",
        "jpackage *",
        "jarsigner *",
        "jstack *",
        "jmap *",
        "jps *",
    ]:
        assert pp["bash"][pattern] == "allow"


def test_denied_actions_can_deny_builtin_read_and_websearch():
    p = build_permission({"denied_actions": ["read", "opencode.builtin.websearch"]}, permission_mode="profile_policy", allow_bash_all=False)
    assert p["read"] == "deny"
    assert p["websearch"] == "deny"
    assert all(not str(k).startswith("efp_") for k in p.keys())


def test_denied_capability_type_tool_denies_builtins():
    p = build_permission({"denied_capability_types": ["tool"]}, permission_mode="profile_policy", allow_bash_all=False)
    assert p["read"] == "deny"
    assert p["write"] == "deny"
    assert p["websearch"] == "deny"


def test_skill_allow_deny_still_works():
    skills = {"skills": [{"opencode_name": "alpha"}]}
    p = build_permission({"allowed_skills": ["alpha"]}, skills_index=skills)
    assert skill_permission_state(p, "alpha") == "allowed"
    p2 = build_permission({"allowed_skills": ["alpha"], "denied_skills": ["alpha"]}, skills_index=skills)
    assert skill_permission_state(p2, "alpha") == "denied"


def test_allowed_external_systems_does_not_generate_tools():
    p = build_permission({"allowed_external_systems": ["jira"]}, skills_index=None)
    text = str(p)
    for marker in ["jira_read_issue", "efp_jira_search", "github_get_pr", "efp_"]:
        assert marker not in text


def test_llm_tools_config_does_not_generate_tools():
    p = build_permission({"llm": {"tools": ["*"]}}, skills_index=None)
    assert set(p.keys()).issuperset({"*", "read", "glob", "grep", "edit", "write", "bash", "external_directory", "webfetch", "websearch", "todowrite", "question", "skill"})
    assert all(not str(k).startswith("efp_") for k in p.keys())


def test_enabled_tools_allowlist_denies_supported_builtins_only():
    p = build_permission({"enabled_tools": ["read", "bash"]})
    assert p["read"] == "allow"
    assert p["bash"]["*"] == "allow"
    for key in ["glob", "grep", "edit", "write", "webfetch", "websearch", "todowrite", "question"]:
        assert p[key] == "deny"
    assert p["skill"]["*"] == "deny"
    assert p["external_directory"] == "allow"
    assert p["*"] == "allow"
    assert all(not str(k).startswith("efp_") for k in p.keys())


def test_disabled_tools_denies_webfetch_and_all_bash_patterns():
    p = build_permission({"disabled_tools": ["webfetch", "bash"]}, permission_mode="profile_policy", allow_bash_all=False)
    assert p["webfetch"] == "deny"
    assert p["bash"]
    assert all(value == "deny" for value in p["bash"].values())


def test_disabled_tools_wins_over_enabled_tools():
    p = build_permission(
        {
            "enabled_tools": ["read", "bash", "webfetch"],
            "tool_permissions": {"read": "allow", "bash": "allow"},
            "disabled_tools": ["read", "bash"],
        }
    )
    assert p["read"] == "deny"
    assert p["webfetch"] == "allow"
    assert all(value == "deny" for value in p["bash"].values())


def test_runtime_v2_tool_aliases_normalize():
    p = build_permission({"enabled_tools": ["read_file", "shell_exec", "todo_write", "web_fetch"]})
    assert p["read"] == "allow"
    assert p["bash"]["*"] == "allow"
    assert p["todowrite"] == "allow"
    assert p["webfetch"] == "allow"
    for key in ["glob", "grep", "edit", "write", "websearch", "question"]:
        assert p[key] == "deny"


def test_tool_permissions_maps_actions_and_ignores_unknowns():
    p = build_permission(
        {
            "tool_permissions": {
                "bash": "ask",
                "read": "allow",
                "edit": "allow",
                "write": {"action": "deny"},
                "skill": "ask",
                "webfetch": "bogus",
                "unknown_tool": "deny",
            }
        },
        permission_mode="profile_policy",
        allow_bash_all=False,
    )
    assert p["bash"]["*"] == "ask"
    assert p["bash"]["git *"] == "allow"
    assert p["read"] == "allow"
    assert p["edit"] == "allow"
    assert p["write"] == "deny"
    assert p["skill"]["*"] == "ask"
    assert p["webfetch"] == "ask"
    assert "unknown_tool" not in p

    denied_bash = build_permission({"tool_permissions": {"shell_exec": "deny"}}, permission_mode="profile_policy", allow_bash_all=False)
    assert all(value == "deny" for value in denied_bash["bash"].values())


def test_tool_permissions_skill_deny_overrides_materialized_named_skill_allow():
    p = build_permission(
        {"allowed_skills": ["alpha"], "tool_permissions": {"skill": "deny"}},
        skills_index={"skills": [{"opencode_name": "alpha"}]},
    )
    assert p["skill"]["*"] == "deny"
    assert p["skill"]["alpha"] == "deny"
    assert skill_permission_state(p, "alpha") == "denied"


def test_empty_enabled_tools_denies_supported_builtin_tools():
    p = build_permission({"enabled_tools": []}, permission_mode="profile_policy", allow_bash_all=False)
    for key in ["read", "glob", "grep", "edit", "write", "webfetch", "websearch", "todowrite", "question"]:
        assert p[key] == "deny"
    assert all(value == "deny" for value in p["bash"].values())
    assert p["skill"]["*"] == "deny"
    assert p["external_directory"] == "allow"
    assert p["*"] == "ask"
