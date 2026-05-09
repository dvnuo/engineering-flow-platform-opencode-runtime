import json

from efp_opencode_adapter.capabilities import load_tools_capabilities
from efp_opencode_adapter.settings import Settings


def test_load_tools_capabilities_exposes_action_alias_and_external_system(tmp_path, monkeypatch):
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    state.mkdir(parents=True)
    (state / "tools-index.json").write_text(json.dumps({"tools": [{
        "capability_id": "efp.tool.jira.jira_search",
        "name": "efp_jira_search",
        "opencode_name": "efp_jira_search",
        "legacy_name": "jira_search",
        "domain": "jira",
        "type": "adapter_action",
        "policy_tags": ["jira", "read_only"],
        "runtime_compat": ["opencode"],
    }]}))

    caps = load_tools_capabilities(Settings.from_env())
    assert len(caps) == 1
    cap = caps[0]
    assert cap["capability_id"] == "efp.tool.jira.jira_search"
    assert cap["name"] == "efp_jira_search"
    assert cap["action_alias"] == "jira_search"
    assert cap["external_system"] == "jira"
    assert cap["adapter_system"] == "jira"
    assert cap["metadata"]["legacy_name"] == "jira_search"
    assert cap["metadata"]["opencode_name"] == "efp_jira_search"
