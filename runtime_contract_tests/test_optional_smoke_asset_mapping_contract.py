import os

import pytest


def _env_first(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def _expected_assets() -> tuple[str, str, str]:
    skill = os.getenv("RUNTIME_CONTRACT_EXPECT_SKILL")
    legacy_tool = _env_first("RUNTIME_CONTRACT_EXPECT_LEGACY_TOOL", "RUNTIME_CONTRACT_EXPECT_TOOL")
    opencode_tool = _env_first("RUNTIME_CONTRACT_EXPECT_OPENCODE_TOOL", "RUNTIME_CONTRACT_EXPECT_EFP_TOOL")
    mapping = os.getenv("RUNTIME_CONTRACT_EXPECT_TOOL_MAPPING")

    if not any([skill, legacy_tool, opencode_tool, mapping]):
        pytest.skip("asset mapping contract disabled")

    assert skill, "RUNTIME_CONTRACT_EXPECT_SKILL is required when asset mapping contract is enabled"

    if mapping:
        assert ":" in mapping, "RUNTIME_CONTRACT_EXPECT_TOOL_MAPPING must be legacy:opencode"
        mapped_legacy, mapped_opencode = mapping.split(":", 1)
        assert mapped_legacy and mapped_opencode
        if legacy_tool:
            assert legacy_tool == mapped_legacy
        if opencode_tool:
            assert opencode_tool == mapped_opencode
        legacy_tool = mapped_legacy
        opencode_tool = mapped_opencode

    assert legacy_tool, "RUNTIME_CONTRACT_EXPECT_LEGACY_TOOL or RUNTIME_CONTRACT_EXPECT_TOOL is required"
    assert opencode_tool, "RUNTIME_CONTRACT_EXPECT_OPENCODE_TOOL or RUNTIME_CONTRACT_EXPECT_EFP_TOOL is required"

    return skill, legacy_tool, opencode_tool


def _find_skill(skills: list[dict], expected_name: str) -> dict | None:
    for item in skills:
        if item.get("name") == expected_name or item.get("opencode_name") == expected_name:
            return item
    return None


def _mapping_available(item: dict, legacy_tool: str, opencode_tool: str) -> bool:
    for mapping in item.get("tool_mappings") or []:
        if (
            mapping.get("efp_name") == legacy_tool
            and mapping.get("opencode_name") == opencode_tool
            and mapping.get("available") is True
        ):
            return True
    return False


def _skill_capability_matches(cap: dict, expected_skill: str) -> bool:
    return cap.get("type") == "skill" and (
        cap.get("name") == expected_skill
        or cap.get("opencode_name") == expected_skill
        or cap.get("metadata", {}).get("opencode_name") == expected_skill
    )


def _cap_value(cap: dict, key: str, default=None):
    if key in cap:
        return cap.get(key)
    metadata = cap.get("metadata")
    if isinstance(metadata, dict):
        return metadata.get(key, default)
    return default


def test_expected_skill_tool_mapping_in_skills_api(get_json):
    skill_name, legacy_tool, opencode_tool = _expected_assets()

    status, body = get_json("/api/skills")
    assert status == 200

    item = _find_skill(body.get("skills", []), skill_name)
    assert item is not None, f"expected skill {skill_name!r} in /api/skills"

    assert legacy_tool in (item.get("tools") or [])
    assert opencode_tool in (item.get("opencode_tools") or [])
    assert _mapping_available(item, legacy_tool, opencode_tool)


def test_expected_skill_tool_mapping_in_capabilities_api(get_json):
    skill_name, legacy_tool, opencode_tool = _expected_assets()

    status, body = get_json("/api/capabilities")
    assert status == 200

    caps = body.get("capabilities") or []
    skill_cap = next((cap for cap in caps if _skill_capability_matches(cap, skill_name)), None)
    assert skill_cap is not None, f"expected skill capability {skill_name!r}"

    opencode_tools = _cap_value(skill_cap, "opencode_tools", [])
    assert opencode_tool in (opencode_tools or [])

    tool_mappings = _cap_value(skill_cap, "tool_mappings", [])
    assert _mapping_available({"tool_mappings": tool_mappings or []}, legacy_tool, opencode_tool)

    tool_cap = next(
        (
            cap
            for cap in caps
            if cap.get("name") == opencode_tool and cap.get("type") in {"tool", "adapter_action", "opencode_tool"}
        ),
        None,
    )
    if tool_cap is None:
        tool_cap = next((cap for cap in caps if cap.get("name") == opencode_tool), None)

    assert tool_cap is not None, f"expected tool capability {opencode_tool!r}"
