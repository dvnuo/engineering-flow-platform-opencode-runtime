import os

import pytest


def test_smoke_asset_mapping_contract_when_enabled(get_json):
    expected_skill = os.getenv("RUNTIME_CONTRACT_EXPECT_SKILL")
    expected_tool = os.getenv("RUNTIME_CONTRACT_EXPECT_TOOL")
    expected_efp_tool = os.getenv("RUNTIME_CONTRACT_EXPECT_EFP_TOOL")

    if not (expected_skill and expected_tool and expected_efp_tool):
        pytest.skip("smoke asset mapping contract disabled")

    _, skills_payload = get_json("/api/skills")
    skills = skills_payload.get("skills", [])
    skill = next((s for s in skills if s.get("name") == expected_skill), None)
    assert skill is not None
    assert expected_tool in skill.get("tools", [])
    assert expected_efp_tool in skill.get("opencode_tools", [])

    mappings = skill.get("tool_mappings", [])
    assert any(
        m.get("efp_name") == expected_tool
        and m.get("opencode_name") == expected_efp_tool
        and m.get("available") is True
        for m in mappings
    )

    _, capabilities_payload = get_json("/api/capabilities")
    caps = capabilities_payload.get("capabilities", [])
    skill_cap = next((c for c in caps if c.get("name") == expected_skill and c.get("type") == "skill"), None)
    assert skill_cap is not None
    opencode_tools = skill_cap.get("opencode_tools") or skill_cap.get("metadata", {}).get("opencode_tools", [])
    assert expected_efp_tool in opencode_tools
