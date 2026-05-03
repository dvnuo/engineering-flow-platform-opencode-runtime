import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings


class FakeOpenCodeClient:
    def __init__(self, auth_success=True):
        self.auth_success = auth_success
        self.auth_calls = []
        self.patch_calls = []

    async def health(self):
        return {"healthy": True, "version": "1.14.29"}

    async def put_auth(self, provider, api_key):
        self.auth_calls.append((provider, api_key))
        return {"success": self.auth_success, "status": 500 if not self.auth_success else 200}

    async def patch_config(self, config):
        self.patch_calls.append(config)
        return {"success": False, "pending_restart": True, "status": 404}


@pytest.mark.asyncio
async def test_apply_contract(tmp_path, monkeypatch):
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    state.mkdir(parents=True)
    (state / "skills-index.json").write_text(json.dumps({"skills": [{"opencode_name": "alpha"}]}))
    (state / "tools-index.json").write_text(json.dumps({"tools": [{"capability_id": "tool.read", "opencode_name": "efp_read", "policy_tags": ["read_only"]}, {"capability_id": "tool.update", "opencode_name": "efp_update", "policy_tags": ["mutation"]}]}))

    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    assert (await client.post("/api/internal/runtime-profile/apply", json={"config": {}})).status == 403
    assert (await client.post("/api/internal/runtime-profile/apply", headers={"X-Portal-Author-Source": "portal"}, json={"config": "bad"})).status == 400

    secret = "SECRET-KEY-SHOULD-NOT-LEAK"
    payload = {"runtime_profile_id": "rp1", "revision": 1, "config": {"allowed_capability_ids": ["opencode.skill.alpha", "tool.read", "tool.update"], "llm": {"provider": "anthropic", "model": "claude-sonnet-4-5", "api_key": secret}}}
    r = await client.post("/api/internal/runtime-profile/apply", headers={"X-Portal-Author-Source": "portal"}, json=payload)
    body = await r.json()
    assert r.status == 200 and body["success"] is True
    assert secret not in json.dumps(body)
    cfg = json.loads((workspace / ".opencode/opencode.json").read_text())
    assert cfg["permission"]["skill"]["alpha"] == "allow"
    assert cfg["permission"]["efp_read"] == "allow"
    assert cfg["permission"]["efp_update"] == "ask"

    payload["config"]["policy_context"] = {"allow_auto_run": True}
    r2 = await client.post("/api/internal/runtime-profile/apply", headers={"X-Portal-Author-Source": "portal"}, json=payload)
    cfg2 = json.loads((workspace / ".opencode/opencode.json").read_text())
    assert (await r2.json())["success"] is True
    assert cfg2["permission"]["efp_update"] == "allow"
    await client.close()


@pytest.mark.asyncio
async def test_apply_auth_failure_warning(tmp_path, monkeypatch):
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient(auth_success=False))
    client = TestClient(TestServer(app))
    await client.start_server()
    secret = "SECRET-KEY-SHOULD-NOT-LEAK"
    r = await client.post("/api/internal/runtime-profile/apply", headers={"X-Portal-Author-Source": "portal"}, json={"runtime_profile_id": "rp1", "revision": 1, "config": {"llm": {"provider": "anthropic", "model": "claude", "api_key": secret}}})
    body = await r.json()
    assert any("auth update failed" in w for w in body["warnings"])
    assert secret not in json.dumps(body)
    await client.close()
