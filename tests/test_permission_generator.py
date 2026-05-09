from efp_opencode_adapter.permission_generator import build_permission, default_permission_baseline, skill_permission_state


def test_baseline_safety():
    p = default_permission_baseline()
    assert p["*"] == "ask"
    assert p["external_directory"] == "deny"
    assert p["skill"]["*"] == "deny"
    assert p["bash"]["rm *"] == "deny"


def test_denied_actions_can_deny_builtin_read_and_websearch():
    p = build_permission({"denied_actions": ["read", "opencode.builtin.websearch"]}, None, None)
    assert p["read"] == "deny"
    assert p["websearch"] == "deny"
    assert p["external_directory"] == "deny"


def test_denied_actions_can_deny_builtin_bash_without_losing_dangerous_rules():
    p = build_permission({"denied_actions": ["bash"]}, None, None)
    assert isinstance(p["bash"], dict)
    assert p["bash"]["*"] == "deny"
    assert p["bash"]["git status*"] == "deny"
    assert p["bash"]["git diff*"] == "deny"
    assert p["bash"]["git log*"] == "deny"
    assert p["bash"]["rm *"] == "deny"
    assert p["bash"]["sudo *"] == "deny"
    assert p["bash"]["git push *"] == "deny"
    assert p["bash"]["curl *|*bash*"] == "deny"


def test_denied_capability_type_tool_denies_builtins():
    p = build_permission({"denied_capability_types": ["tool"]}, None, None)
    for key in ("read", "glob", "grep", "edit", "write", "webfetch", "websearch", "todowrite", "question"):
        assert p[key] == "deny"
    assert p["bash"]["*"] == "deny"
    assert p["bash"]["rm *"] == "deny"


def test_denied_capability_type_shell_denies_bash_only():
    p = build_permission({"denied_capability_types": ["shell"]}, None, None)
    assert p["bash"]["*"] == "deny"
    assert p["read"] == "allow"


def test_skill_allow_unknown_deny_and_type_override():
    perm = build_permission({"allowed_capability_ids": ["opencode.skill.alpha"]}, {"skills": [{"opencode_name": "alpha"}]}, None)
    assert perm["skill"]["alpha"] == "allow"
    assert "beta" not in perm["skill"] and perm["skill"]["*"] == "deny"
    denied = build_permission({"allowed_capability_ids": ["opencode.skill.alpha"], "denied_capability_types": ["skill"]}, {"skills": [{"opencode_name": "alpha"}]}, None)
    assert denied["skill"]["alpha"] == "deny"


def test_allowed_capability_type_skill_allows_known_skills_only():
    p = build_permission({"allowed_capability_types": ["skill"]}, {"skills": [{"opencode_name": "alpha"}]}, None)
    assert p["skill"]["alpha"] == "allow"
    assert "beta" not in p["skill"]


def test_derived_runtime_rules_denied_actions_override_allow():
    tools = {"tools": [{"capability_id": "tool.a", "name": "efp_a", "policy_tags": ["read_only"]}]}
    p = build_permission({"allowed_capability_ids": ["tool.a"], "derived_runtime_rules": {"denied_actions": ["tool.a"]}}, None, tools)
    assert p["efp_a"] == "deny"


def test_policy_context_denied_capability_types_override_allow():
    tools = {"tools": [{"capability_id": "tool.a", "name": "efp_a", "type": "adapter_action", "policy_tags": ["read_only"]}]}
    p = build_permission({"allowed_capability_ids": ["tool.a"], "policy_context": {"denied_capability_types": ["adapter_action"]}}, None, tools)
    assert p["efp_a"] == "deny"


def test_llm_tools_dict_supports_allowed_capability_types_and_external_systems():
    tools = {"tools": [{"capability_id": "tool.github.read", "name": "efp_github_read", "domain": "github", "policy_tags": ["read_only", "github"]}, {"capability_id": "tool.jira.read", "name": "efp_jira_read", "domain": "jira", "policy_tags": ["read_only", "jira"]}]}
    p = build_permission({"llm": {"tools": {"allowed_capability_types": ["adapter_action"], "allowed_external_systems": ["jira"]}}}, None, tools)
    assert p["efp_jira_read"] == "allow"
    assert p["efp_github_read"] == "deny"


def test_tool_loop_no_auto_allow_mutation_and_other_rules():
    tools = {"tools": [{"capability_id": "tool.upd", "name": "efp_upd", "policy_tags": ["mutation"]}]}
    p = build_permission({"llm": {"tools": ["tool.upd"], "tool_loop": True}}, None, tools)
    assert p["efp_upd"] == "ask"


def test_runtime_compat_and_opencode_name_priority():
    tools = {"tools": [{"capability_id": "tool.native", "name": "native_read", "opencode_name": "efp_native_read", "policy_tags": ["read_only"], "runtime_compat": ["native"]}, {"capability_id": "tool.github.get_pr", "name": "github_get_pr", "opencode_name": "efp_github_get_pr", "policy_tags": ["read_only"], "runtime_compat": ["opencode"]}]}
    p = build_permission({"allowed_capability_types": ["adapter_action"], "allowed_capability_ids": ["tool.github.get_pr", "tool.native"]}, None, tools)
    assert p["efp_github_get_pr"] == "allow"
    assert "github_get_pr" not in p and "efp_native_read" not in p


def test_denied_builtin_skill_action_overrides_allowed_skill():
    p = build_permission({"allowed_capability_ids": ["opencode.skill.alpha"], "denied_actions": ["opencode.builtin.skill"]}, {"skills": [{"opencode_name": "alpha"}]}, None)
    assert p["skill"]["alpha"] == "deny"
    assert p["skill"]["*"] == "deny"


def test_denied_skill_action_overrides_allowed_skill():
    p = build_permission({"allowed_capability_ids": ["opencode.skill.alpha"], "denied_actions": ["skill"]}, {"skills": [{"opencode_name": "alpha"}]}, None)
    assert p["skill"]["alpha"] == "deny"
    assert p["skill"]["*"] == "deny"


def test_generated_tool_cannot_override_todowrite_or_question_builtins():
    tools = {"tools": [{"capability_id": "tool.todo", "name": "todowrite", "policy_tags": ["read_only"]}, {"capability_id": "tool.question", "name": "question", "policy_tags": ["read_only"]}]}
    p = build_permission({"allowed_capability_ids": ["tool.todo", "tool.question"]}, None, tools)
    assert p["todowrite"] == "ask"
    assert p["question"] == "ask"


def test_denied_capability_type_shell_denies_all_bash_patterns():
    p = build_permission({"denied_capability_types": ["shell"]}, None, None)
    assert p["bash"]["*"] == "deny"
    assert p["bash"]["git status*"] == "deny"
    assert p["bash"]["git diff*"] == "deny"
    assert p["bash"]["git log*"] == "deny"
    assert p["bash"]["rm *"] == "deny"
    assert p["bash"]["sudo *"] == "deny"
    assert p["bash"]["git push *"] == "deny"
    assert p["bash"]["curl *|*bash*"] == "deny"


def test_denied_capability_type_tool_denies_all_bash_patterns():
    p = build_permission({"denied_capability_types": ["tool"]}, None, None)
    assert p["bash"]["*"] == "deny"
    assert p["bash"]["git status*"] == "deny"
    assert p["bash"]["git diff*"] == "deny"
    assert p["bash"]["git log*"] == "deny"


def test_denied_capability_type_tool_overrides_allowed_skill():
    p = build_permission({"allowed_capability_ids": ["opencode.skill.alpha"], "denied_capability_types": ["tool"]}, {"skills": [{"opencode_name": "alpha"}]}, None)
    assert p["skill"]["alpha"] == "deny"
    assert p["skill"]["*"] == "deny"


def test_denied_actions_opencode_builtin_bash_denies_all_bash_patterns():
    p = build_permission({"denied_actions": ["opencode.builtin.bash"]}, None, None)
    assert p["bash"]["*"] == "deny"
    assert p["bash"]["git status*"] == "deny"
    assert p["bash"]["git diff*"] == "deny"
    assert p["bash"]["git log*"] == "deny"


def test_allowed_skills_and_denied_skills_and_aliases():
    skills = {"skills": [{"opencode_name": "review-pull-request", "efp_name": "review_pull_request"}]}
    p1 = build_permission({"allowed_skills": ["review-pull-request"]}, skills, None)
    assert p1["skill"]["review-pull-request"] == "allow"
    p2 = build_permission({"allowed_skills": ["review_pull_request"]}, skills, None)
    assert p2["skill"]["review-pull-request"] == "allow"
    p3 = build_permission({"allowed_skills": ["review-pull-request"], "denied_skills": ["review-pull-request"]}, skills, None)
    assert p3["skill"]["review-pull-request"] == "deny"
    p4 = build_permission({"capability_profile": {"skill_set": ["review-pull-request"]}}, skills, None)
    assert p4["skill"]["review-pull-request"] == "allow"


def test_skill_permission_state_supports_scalar_and_aliases():
    assert skill_permission_state({"skill": "allow"}, "my-skill") == "allowed"
    assert skill_permission_state({"skill": {"skill:my-skill": "allow"}}, "my-skill") == "allowed"
    assert skill_permission_state({"skill": {"opencode.skill.my_skill": "deny"}}, "my-skill") == "denied"
    assert skill_permission_state({"skill": {"*": "deny"}}, "my-skill") == "denied"


def test_llm_tools_wildcard_allows_generated_jira_read_tool():
    tools = {"tools": [{
        "capability_id": "efp.tool.jira.jira_search",
        "tool_id": "efp.tool.jira.jira_search",
        "name": "efp_jira_search",
        "opencode_name": "efp_jira_search",
        "legacy_name": "jira_search",
        "domain": "jira",
        "type": "adapter_action",
        "policy_tags": ["jira", "read_only"],
        "runtime_compat": ["opencode"],
    }]}
    p = build_permission({"llm": {"tools": ["*"]}}, None, tools)
    assert p["efp_jira_search"] == "allow"


def test_tool_set_fallback_tool_alias_allows_jira_wrapper():
    tools = {"tools": [{
        "capability_id": "efp.tool.jira.jira_search",
        "tool_id": "efp.tool.jira.jira_search",
        "name": "efp_jira_search",
        "opencode_name": "efp_jira_search",
        "legacy_name": "jira_search",
        "domain": "jira",
        "type": "adapter_action",
        "policy_tags": ["jira", "read_only"],
        "runtime_compat": ["opencode"],
    }]}
    p = build_permission({"allowed_capability_ids": ["tool:jira_search"]}, None, tools)
    assert p["efp_jira_search"] == "allow"


def test_portal_seed_jira_read_issue_alias_allows_jira_read_only_wrapper_only():
    tools = {"tools": [
        {
            "capability_id": "efp.tool.jira.jira_search",
            "tool_id": "efp.tool.jira.jira_search",
            "name": "efp_jira_search",
            "opencode_name": "efp_jira_search",
            "legacy_name": "jira_search",
            "domain": "jira",
            "type": "adapter_action",
            "policy_tags": ["jira", "read_only"],
            "runtime_compat": ["opencode"],
        },
        {
            "capability_id": "efp.tool.github.github_get_pr",
            "tool_id": "efp.tool.github.github_get_pr",
            "name": "efp_github_get_pr",
            "opencode_name": "efp_github_get_pr",
            "legacy_name": "github_get_pr",
            "domain": "github",
            "type": "adapter_action",
            "policy_tags": ["github", "read_only"],
            "runtime_compat": ["opencode"],
        },
    ]}
    p = build_permission(
        {
            "allowed_capability_ids": ["adapter:jira:read_issue"],
            "allowed_adapter_actions": ["adapter:jira:read_issue"],
            "allowed_external_systems": ["jira"],
        },
        None,
        tools,
    )
    assert p["efp_jira_search"] == "allow"
    assert "efp_github_get_pr" not in p or p["efp_github_get_pr"] == "deny"


def test_deny_overrides_wildcard_generated_tool_allow():
    tools = {"tools": [{
        "capability_id": "efp.tool.jira.jira_search",
        "name": "efp_jira_search",
        "legacy_name": "jira_search",
        "domain": "jira",
        "type": "adapter_action",
        "policy_tags": ["jira", "read_only"],
        "runtime_compat": ["opencode"],
    }]}
    p = build_permission(
        {
            "llm": {"tools": ["*"]},
            "derived_runtime_rules": {"denied_actions": ["jira_search"]},
        },
        None,
        tools,
    )
    assert p["efp_jira_search"] == "deny"
