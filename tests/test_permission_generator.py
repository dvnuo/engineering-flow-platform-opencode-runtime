from efp_opencode_adapter.permission_generator import build_permission, default_permission_baseline


def test_baseline_safety():
    p = default_permission_baseline()
    assert p["*"] == "ask"
    assert p["external_directory"] == "deny"
    assert p["skill"]["*"] == "deny"
    assert p["bash"]["rm *"] == "deny"


def test_skill_allow_unknown_deny():
    perm = build_permission({"allowed_capability_ids": ["opencode.skill.alpha"]}, {"skills": [{"opencode_name": "alpha"}]}, None)
    assert perm["skill"]["alpha"] == "allow"
    assert "beta" not in perm["skill"] and perm["skill"]["*"] == "deny"


def test_llm_tools_list_and_dict_and_tool_loop_no_auto_allow():
    tools = {"tools": [{"capability_id": "tool.read", "name": "efp_read", "policy_tags": ["read_only"]}, {"capability_id": "tool.upd", "name": "efp_upd", "policy_tags": ["mutation"]}]}
    p1 = build_permission({"llm": {"tools": ["tool.read", "tool.upd"], "tool_loop": True}}, None, tools)
    assert p1["efp_read"] == "allow"
    assert p1["efp_upd"] == "ask"
    p2 = build_permission({"llm": {"tools": {"allow": ["tool.read"], "deny": ["tool.read"]}}}, None, tools)
    assert p2["efp_read"] == "deny"


def test_denied_even_when_not_allowed_and_unsafe_always_deny():
    tools = {"tools": [{"capability_id": "tool.a", "name": "efp_a", "type": "adapter_action", "policy_tags": ["read_only"]}, {"capability_id": "tool.u", "name": "efp_u", "policy_tags": ["unsafe", "read_only"]}]}
    p = build_permission({"denied_actions": ["tool.a"], "denied_capability_types": ["adapter_action"]}, None, tools)
    assert p["efp_a"] == "deny"
    assert p["efp_u"] == "deny"


def test_reserved_bash_not_overridden():
    tools = {"tools": [{"capability_id": "tool.b", "name": "bash", "policy_tags": ["read_only"]}]}
    p = build_permission({"allowed_capability_ids": ["tool.b"]}, None, tools)
    assert isinstance(p["bash"], dict)
    assert p["bash"]["rm *"] == "deny"
    assert p["bash"]["sudo *"] == "deny"
    assert p["bash"]["git push *"] == "deny"
