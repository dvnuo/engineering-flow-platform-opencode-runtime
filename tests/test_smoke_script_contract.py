from pathlib import Path


def test_smoke_script_asserts_skill_tool_mapping_contract():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "smoke.sh").read_text(encoding="utf-8")
    assert "legacy_name" in script
    assert "smoke_tool" in script
    assert "efp_smoke_tool" in script
    assert "smoke_tool -> efp_smoke_tool" in script
    assert "opencode_tools" in script
    assert "tool_mappings" in script
