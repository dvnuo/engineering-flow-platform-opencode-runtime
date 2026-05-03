import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings


class FakeClient:
    async def health(self):
        return {"healthy": True, "version": "1.14.29"}

    async def mcp(self):
        return {"success": True, "tools": [{"name": "github_status", "description": "GitHub status", "inputSchema": {"type": "object"}}]}


@pytest.mark.asyncio
async def test_capabilities_catalog(tmp_path, monkeypatch):
    workspace, state, tools, skills = tmp_path / "workspace", tmp_path / "state", tmp_path / "tools", tmp_path / "skills"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("EFP_TOOLS_DIR", str(tools))
    monkeypatch.setenv("EFP_SKILLS_DIR", str(skills))
    monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    state.mkdir(parents=True)
    tools.mkdir(parents=True)
    (workspace / ".opencode").mkdir(parents=True)
    (state / "skills-index.json").write_text(json.dumps({"skills": [{"opencode_name": "my-skill", "description": "d", "efp_name": "e", "tools": [], "task_tools": []}]}))
    (tools / "manifest.yaml").write_text("tools:\n  - capability_id: tool.read\n    opencode_name: efp_read\n    policy_tags: [read_only]\n")
    (workspace / ".opencode/opencode.json").write_text(json.dumps({"agent": {"efp-main": {"description": "Main"}}, "api_key": "SECRET"}))

    app = create_app(Settings.from_env(), opencode_client=FakeClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    payload = await (await client.get("/api/capabilities")).json()
    caps = payload["capabilities"]
    names = {c.get("name") for c in caps}
    assert {"read", "bash", "websearch", "my-skill", "efp_read", "efp-main", "github_status"}.issubset(names)
    mcp = next(c for c in caps if c.get("name") == "github_status")
    assert mcp["capability_id"] == "opencode.mcp.github_status"
    assert mcp["type"] == "mcp_tool"
    assert mcp["source_ref"] == "opencode_mcp"
    assert "mcp" in mcp["policy_tags"]
    for c in caps:
        for key in ("capability_id", "type", "name", "enabled", "policy_tags", "source_ref"):
            assert key in c
    assert payload["count"] == len(caps)
    assert payload["catalog_version"]
    assert payload["supports_snapshot_contract"] is True
    assert payload["runtime_contract_version"] == "efp-opencode-compat-v1"
    encoded = json.dumps(payload)
    assert "SECRET" not in encoded and "api_key" not in encoded and "token" not in encoded
    await client.close()
