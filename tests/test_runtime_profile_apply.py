import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings


class FakeOpenCodeClient:
    def __init__(self):
        self.auth_calls = []
        self.patch_calls = []

    async def health(self):
        return {"healthy": True, "version": "1.14.29"}

    async def put_auth(self, provider, api_key):
        self.auth_calls.append((provider, api_key))
        return {"success": True, "status": 200}

    async def patch_config(self, config):
        self.patch_calls.append(config)
        return {"success": False, "pending_restart": True, "status": 404}


@pytest.mark.asyncio
async def test_apply_contract(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    settings = Settings.from_env()
    fake = FakeOpenCodeClient()
    app = create_app(settings, opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()

    r1 = await client.post("/api/internal/runtime-profile/apply", json={"config": {}})
    assert r1.status == 403

    r2 = await client.post("/api/internal/runtime-profile/apply", headers={"X-Portal-Author-Source": "portal"}, json={"config": "bad"})
    assert r2.status == 400

    secret = "SECRET-KEY-SHOULD-NOT-LEAK"
    payload = {"runtime_profile_id": "rp1", "revision": 1, "config": {"llm": {"provider": "anthropic", "model": "claude-sonnet-4-5", "api_key": secret}}}
    r3 = await client.post("/api/internal/runtime-profile/apply", headers={"X-Portal-Author-Source": "portal"}, json=payload)
    body = await r3.json()
    assert r3.status == 200
    assert body["success"] is True and body["engine"] == "opencode"
    assert body["runtime_profile_id"] == "rp1" and body["revision"] == 1
    assert {"llm", "permission", "agent"}.issubset(set(body["updated_sections"]))
    assert secret not in json.dumps(body)
    overlay = json.loads((state / "runtime-profile-overlay.json").read_text())
    assert overlay["generated_config_hash"] == body["config_hash"]
    cfg_text = (workspace / ".opencode/opencode.json").read_text()
    assert secret not in cfg_text
    cfg = json.loads(cfg_text)
    assert cfg["agent"]["efp-main"]["model"] == "anthropic/claude-sonnet-4-5"
    assert fake.auth_calls[0][0] == "anthropic"
    assert body["pending_restart"] is True
    await client.close()
