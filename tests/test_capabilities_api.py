import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings


class FakeClient:
    async def health(self):
        return {"healthy": True, "version": "1.14.29"}

    async def mcp(self):
        return {"success": False, "tools": []}


@pytest.mark.asyncio
async def test_capabilities_catalog(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    tools = tmp_path / "tools"
    skills = tmp_path / "skills"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("EFP_TOOLS_DIR", str(tools))
    monkeypatch.setenv("EFP_SKILLS_DIR", str(skills))
    monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    state.mkdir(parents=True)
    (workspace / ".opencode").mkdir(parents=True)
    (state / "skills-index.json").write_text(json.dumps({"skills": [{"opencode_name": "my-skill", "description": "d", "efp_name": "e", "tools": [], "task_tools": []}]}))
    (state / "tools-index.json").write_text(json.dumps({"tools": [{"capability_id": "tool.read", "opencode_name": "efp_read", "policy_tags": ["read_only"]}]}))
    (workspace / ".opencode/opencode.json").write_text(json.dumps({"agent": {"efp-main": {"description": "Main"}}, "api_key": "SECRET"}))

    app = create_app(Settings.from_env(), opencode_client=FakeClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    resp = await client.get("/api/capabilities")
    payload = await resp.json()
    assert resp.status == 200
    names = {c.get("name") for c in payload["capabilities"]}
    assert {"read", "bash", "websearch", "my-skill", "efp_read", "efp-main"}.issubset(names)
    assert payload["count"] == len(payload["capabilities"])
    assert payload["catalog_version"]
    assert payload["supports_snapshot_contract"] is True
    assert payload["runtime_contract_version"] == "efp-opencode-compat-v1"
    assert "SECRET" not in json.dumps(payload)
    await client.close()
