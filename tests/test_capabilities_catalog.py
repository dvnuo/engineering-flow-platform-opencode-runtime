import json

from efp_opencode_adapter.capabilities import load_skills_capabilities
from efp_opencode_adapter.settings import Settings


def test_load_skills_capabilities_filters_removed_external_tool_fields(tmp_path, monkeypatch):
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    (workspace / ".opencode").mkdir(parents=True)
    state.mkdir(parents=True)
    (workspace / ".opencode/opencode.json").write_text(json.dumps({"permission": {"skill": {"*": "allow"}}}), encoding="utf-8")
    (state / "skills-index.json").write_text(json.dumps({"skills": [{"opencode_name": "demo", "tool_mappings": [{"efp_name": "a"}], "opencode_tools": ["efp_a"], "missing_tools": ["a"], "missing_opencode_tools": ["efp_a"]}]}), encoding="utf-8")

    caps = load_skills_capabilities(Settings.from_env())
    assert len(caps) == 1
    cap = caps[0]
    assert cap["name"] == "demo"
    for key in ("tool_mappings", "opencode_tools", "missing_tools", "missing_opencode_tools"):
        assert key not in cap
        assert key not in cap.get("metadata", {})
