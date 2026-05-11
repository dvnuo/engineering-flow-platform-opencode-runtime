from efp_opencode_adapter.permission_generator import build_permission, default_permission_baseline, skill_permission_state


def test_baseline_safety():
    p = default_permission_baseline()
    assert p["*"] == "allow"
    assert p["external_directory"] == "deny"
    assert p["skill"]["*"] == "allow"
    assert p["bash"] == {"*": "allow"}


def test_denied_actions_can_deny_builtin_read_and_websearch():
    p = build_permission({"denied_actions": ["read", "opencode.builtin.websearch"]}, None, None, permission_mode="profile_policy", allow_bash_all=False)
    assert p["read"] == "deny"
    assert p["websearch"] == "deny"
    assert p["external_directory"] == "deny"


def test_denied_actions_can_deny_builtin_bash_without_losing_dangerous_rules():
    p = build_permission({"denied_actions": ["bash"]}, None, None, permission_mode="profile_policy", allow_bash_all=False)
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
    p = build_permission({"denied_capability_types": ["tool"]}, None, None, permission_mode="profile_policy", allow_bash_all=False)
    for key in ("read", "glob", "grep", "edit", "write", "webfetch", "websearch", "todowrite", "question"):
        assert p[key] == "deny"
    assert p["bash"]["*"] == "deny"
    assert p["bash"]["rm *"] == "deny"


def test_denied_capability_type_shell_denies_bash_only():
    p = build_permission({"denied_capability_types": ["shell"]}, None, None, permission_mode="profile_policy", allow_bash_all=False)
    assert p["bash"]["*"] == "deny"
    assert p["read"] == "allow"


def test_skill_allow_unknown_deny_and_type_override():
    perm = build_permission({"allowed_capability_ids": ["opencode.skill.alpha"]}, {"skills": [{"opencode_name": "alpha"}]}, None)
    assert perm["skill"]["alpha"] == "allow"
    assert perm["skill"]["*"] == "allow"
    assert skill_permission_state(perm, "beta") == "allowed"
    denied = build_permission({"allowed_capability_ids": ["opencode.skill.alpha"], "denied_capability_types": ["skill"]}, {"skills": [{"opencode_name": "alpha"}]}, None)
    assert denied["skill"]["alpha"] == "deny"


def test_allowed_capability_type_skill_writes_known_skill_allow_while_wildcard_allows_unknown():
    p = build_permission({"allowed_capability_types": ["skill"]}, {"skills": [{"opencode_name": "alpha"}]}, None)
    assert p["skill"]["alpha"] == "allow"
    assert p["skill"]["*"] == "allow"
    assert skill_permission_state(p, "beta") == "allowed"


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
    p = build_permission({"llm": {"tools": ["tool.upd"], "tool_loop": True}}, None, tools, permission_mode="profile_policy", allow_bash_all=False)
    assert p["efp_upd"] == "ask"


def test_permission_generator_honors_descriptor_permission_default():
    tools = {"tools": [
        {"capability_id": "tool.ask", "opencode_name": "efp_ask", "policy_tags": ["mutation"], "permission_default": "ask"},
        {"capability_id": "tool.deny", "opencode_name": "efp_deny", "policy_tags": ["mutation"], "permission_default": "deny"},
        {"capability_id": "tool.read", "opencode_name": "efp_read", "policy_tags": ["read_only"]},
    ]}
    p = build_permission({"allowed_capability_ids": ["tool.ask", "tool.deny", "tool.read"]}, None, tools, permission_mode="workspace_full_access")
    assert p["efp_ask"] == "allow"
    assert p["efp_deny"] == "deny"
    assert p["efp_read"] == "allow"
    p2 = build_permission({"denied_actions": ["tool.ask"], "allowed_capability_ids": ["tool.ask"]}, None, tools, permission_mode="workspace_full_access")
    assert p2["efp_ask"] == "deny"


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
    p = build_permission({"allowed_capability_ids": ["tool.todo", "tool.question"]}, None, tools, permission_mode="profile_policy", allow_bash_all=False)
    assert p["todowrite"] == "ask"
    assert p["question"] == "ask"


def test_denied_capability_type_shell_denies_all_bash_patterns():
    p = build_permission({"denied_capability_types": ["shell"]}, None, None, permission_mode="profile_policy", allow_bash_all=False)
    assert p["bash"]["*"] == "deny"
    assert p["bash"]["git status*"] == "deny"
    assert p["bash"]["git diff*"] == "deny"
    assert p["bash"]["git log*"] == "deny"
    assert p["bash"]["rm *"] == "deny"
    assert p["bash"]["sudo *"] == "deny"
    assert p["bash"]["git push *"] == "deny"
    assert p["bash"]["curl *|*bash*"] == "deny"


def test_denied_capability_type_tool_denies_all_bash_patterns():
    p = build_permission({"denied_capability_types": ["tool"]}, None, None, permission_mode="profile_policy", allow_bash_all=False)
    assert p["bash"]["*"] == "deny"
    assert p["bash"]["git status*"] == "deny"
    assert p["bash"]["git diff*"] == "deny"
    assert p["bash"]["git log*"] == "deny"


def test_denied_capability_type_tool_overrides_allowed_skill():
    p = build_permission({"allowed_capability_ids": ["opencode.skill.alpha"], "denied_capability_types": ["tool"]}, {"skills": [{"opencode_name": "alpha"}]}, None)
    assert p["skill"]["alpha"] == "deny"
    assert p["skill"]["*"] == "deny"


def test_denied_actions_opencode_builtin_bash_denies_all_bash_patterns():
    p = build_permission({"denied_actions": ["opencode.builtin.bash"]}, None, None, permission_mode="profile_policy", allow_bash_all=False)
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




def test_all_known_skills_default_to_allow():
    skills = {
        "skills": [
            {"opencode_name": "skill-mode-demo", "efp_name": "skill_mode_demo"},
            {"opencode_name": "review-pull-request", "efp_name": "review_pull_request"},
        ]
    }

    p = build_permission({"llm": {"tools": ["*"]}}, skills, None)

    assert p["skill"]["*"] == "allow"
    assert p["skill"]["skill-mode-demo"] == "allow"
    assert p["skill"]["review-pull-request"] == "allow"
    assert skill_permission_state(p, "skill-mode-demo") == "allowed"
    assert skill_permission_state(p, "review_pull_request") == "allowed"
    assert skill_permission_state(p, "some-new-skill") == "allowed"


def test_capability_profile_skill_set_wildcard_allows_all_skills():
    skills = {"skills": [{"opencode_name": "alpha"}, {"opencode_name": "beta"}]}

    p = build_permission({"capability_profile": {"skill_set": ["*"]}}, skills, None)

    assert p["skill"]["*"] == "allow"
    assert p["skill"]["alpha"] == "allow"
    assert p["skill"]["beta"] == "allow"


def test_denied_skill_overrides_default_allow():
    skills = {"skills": [{"opencode_name": "alpha"}, {"opencode_name": "beta"}]}

    p = build_permission({"denied_skills": ["alpha"]}, skills, None)

    assert p["skill"]["*"] == "allow"
    assert p["skill"]["alpha"] == "deny"
    assert p["skill"]["beta"] == "allow"
    assert skill_permission_state(p, "alpha") == "denied"
    assert skill_permission_state(p, "beta") == "allowed"


def test_denied_skills_wildcard_overrides_default_allow():
    skills = {"skills": [{"opencode_name": "alpha"}]}

    p = build_permission({"denied_skills": ["*"]}, skills, None)

    assert p["skill"]["*"] == "deny"
    assert p["skill"]["alpha"] == "deny"
    assert skill_permission_state(p, "alpha") == "denied"


def test_denied_capability_type_skill_denies_skill_wildcard():
    p = build_permission({"denied_capability_types": ["skill"]}, {"skills": []}, None)

    assert p["skill"]["*"] == "deny"
    assert skill_permission_state(p, "any-skill") == "denied"

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


TOOLS_INDEX = {
    "tools": [
        {
            "capability_id": "adapter:jira:read_issue",
            "opencode_name": "jira_read_issue",
            "type": "adapter_action",
            "policy_tags": ["read", "jira"],
            "runtime_compat": ["opencode"],
        },
        {
            "capability_id": "adapter:jira:add_comment",
            "opencode_name": "jira_add_comment",
            "type": "adapter_action",
            "policy_tags": ["comment", "jira"],
            "runtime_compat": ["opencode"],
        },
        {
            "capability_id": "adapter:jira:dangerous_delete",
            "opencode_name": "jira_dangerous_delete",
            "type": "adapter_action",
            "policy_tags": ["unsafe", "jira"],
            "runtime_compat": ["opencode"],
        },
    ]
}


def test_missing_llm_tools_defaults_to_all_generated_tools():
    permission = build_permission({}, tools_index=TOOLS_INDEX)
    assert permission["jira_read_issue"] == "allow"
    assert permission["jira_add_comment"] == "allow"
    assert permission["jira_dangerous_delete"] == "deny"


def test_llm_present_but_missing_tools_key_still_defaults_to_all_generated_tools():
    permission = build_permission({"llm": {}}, tools_index=TOOLS_INDEX)
    assert permission["jira_read_issue"] == "allow"
    assert permission["jira_add_comment"] == "allow"


def test_llm_tools_wildcard_still_allows_all_generated_tools():
    permission = build_permission({"llm": {"tools": ["*"]}}, tools_index=TOOLS_INDEX)
    assert permission["jira_read_issue"] == "allow"
    assert permission["jira_add_comment"] == "allow"


def test_explicit_empty_llm_tools_does_not_trigger_missing_fallback():
    permission = build_permission({"llm": {"tools": []}}, tools_index=TOOLS_INDEX)
    assert "jira_read_issue" not in permission
    assert "jira_add_comment" not in permission
    assert permission.get("jira_dangerous_delete") in (None, "deny")


def test_generated_tool_deny_still_overrides_missing_llm_tools_default_all():
    permission = build_permission(
        {
            "llm": {},
            "derived_runtime_rules": {
                "denied_actions": ["adapter:jira:add_comment", "jira_add_comment"]
            },
        },
        tools_index=TOOLS_INDEX,
    )
    assert permission["jira_read_issue"] == "allow"
    assert permission["jira_add_comment"] == "deny"


def test_external_system_restriction_still_overrides_missing_llm_tools_default_all():
    permission = build_permission(
        {
            "llm": {},
            "allowed_external_systems": ["github"],
        },
        tools_index=TOOLS_INDEX,
    )
    assert permission["jira_read_issue"] == "deny"


def test_workspace_full_access_baseline():
    p = default_permission_baseline(permission_mode="workspace_full_access", allow_bash_all=True)
    assert p["*"] == "allow"
    for k in ("read","glob","grep","edit","write","webfetch","websearch","todowrite","question"):
        assert p[k] == "allow"
    assert p["bash"] == {"*": "allow"}
    assert "rm *" not in p["bash"]
    assert p["skill"]["*"] == "allow"
    assert p["external_directory"] == "deny"


def test_profile_policy_baseline_keeps_old_behavior():
    p = default_permission_baseline(permission_mode="profile_policy", allow_bash_all=False)
    assert p["*"] == "ask"
    assert p["edit"] == "ask"
    assert p["bash"]["rm *"] == "deny"


def test_workspace_full_access_mutation_tool_is_allow():
    tools = {"tools": [{"capability_id": "tool.upd", "name": "efp_upd", "policy_tags": ["mutation"]}]}
    p = build_permission({"llm": {"tools": ["tool.upd"]}}, None, tools, permission_mode="workspace_full_access")
    assert p["efp_upd"] == "allow"


def test_default_baseline_and_build_permission_default_to_workspace_full_access():
    baseline = default_permission_baseline()
    built = build_permission({}, None, None)
    assert baseline["edit"] == "allow" and baseline["write"] == "allow"
    assert baseline["bash"] == {"*": "allow"}
    assert built["edit"] == "allow" and built["write"] == "allow"
    assert built["bash"] == {"*": "allow"}


def test_profile_policy_with_allow_bash_all_false_keeps_legacy_bash_denies():
    p = build_permission({}, None, None, permission_mode="profile_policy", allow_bash_all=False)
    assert p["bash"]["rm *"] == "deny"
    assert p["bash"]["sudo *"] == "deny"


def test_full_access_bash_allow_has_no_legacy_deny_patterns():
    p = build_permission({}, None, None, permission_mode="workspace_full_access", allow_bash_all=True)
    assert p["bash"] == {"*": "allow"}
