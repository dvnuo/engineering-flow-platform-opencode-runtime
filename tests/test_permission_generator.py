from efp_opencode_adapter.permission_generator import build_permission, default_permission_baseline


def test_baseline_safety():
    p = default_permission_baseline()
    assert p["*"] == "ask"
    assert p["external_directory"] == "deny"
    assert p["skill"]["*"] == "deny"
    assert p["bash"]["rm *"] == "deny"
    assert p["bash"]["sudo *"] == "deny"


def test_skill_allow_and_unknown_deny():
    perm = build_permission({"allowed_capability_ids": ["opencode.skill.alpha"]}, {"skills": [{"opencode_name": "alpha"}]}, None)
    assert perm["skill"]["alpha"] == "allow"
    assert "beta" not in perm["skill"]


def test_mutation_default_ask_and_auto_allow_and_deny_override():
    tools = {"tools": [{"capability_id": "tool1", "opencode_name": "efp_update", "policy_tags": ["mutation"]}]}
    p1 = build_permission({"allowed_capability_ids": ["tool1"]}, None, tools)
    assert p1["efp_update"] == "ask"
    p2 = build_permission({"allowed_capability_ids": ["tool1"], "policy_context": {"allow_auto_run": True}}, None, tools)
    assert p2["efp_update"] == "allow"
    p3 = build_permission({"allowed_capability_ids": ["tool1"], "denied_actions": ["tool1"], "policy_context": {"allow_auto_run": True}}, None, tools)
    assert p3["efp_update"] == "deny"
