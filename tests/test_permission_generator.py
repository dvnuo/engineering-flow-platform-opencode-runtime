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
    assert ws["external_directory"] == "deny"

    pp = default_permission_baseline(permission_mode="profile_policy", allow_bash_all=False)
    assert pp["*"] == "ask"
    assert pp["edit"] == "ask" and pp["write"] == "ask"
    assert pp["bash"]["rm *"] == "deny"


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
