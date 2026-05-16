import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings


class FakeOpenCodeClient:
    async def health(self):
        return {"healthy": True, "version": "1.14.39"}

    async def put_auth_info(self, provider, auth_info):
        return {"success": True, "status": 200}

    async def put_auth(self, provider, api_key):
        return {"success": True, "status": 200}

    async def patch_config(self, config):
        return {"success": True, "status": 200}


@pytest.mark.asyncio
async def test_runtime_profile_apply_writes_atlassian_config_and_env(tmp_path, monkeypatch):
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    config_path = tmp_path / "home" / ".config" / "atlassian" / "config.json"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    monkeypatch.setenv("ATLASSIAN_CONFIG", str(config_path))

    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    password = "jira-password-secret"
    token = "confluence-token-secret"
    resp = await client.post(
        "/api/internal/runtime-profile/apply",
        headers={"X-Portal-Author-Source": "portal"},
        json={
            "runtime_profile_id": "rp-atlassian",
            "revision": 3,
            "config": {
                "jira": {"enabled": True, "instances": [{"name": "jira-main", "url": "https://jira.example", "username": "svc", "password": password}]},
                "confluence": {"enabled": True, "instances": [{"name": "docs", "url": "https://docs.example", "token": token, "space": "ENG"}]},
            },
        },
    )
    body = await resp.json()

    assert resp.status == 200
    assert body["atlassian_cli_configured"] is True
    assert body["atlassian_config_path"] == str(config_path)
    assert body["atlassian_jira_instances"] == 1
    assert body["atlassian_confluence_instances"] == 1
    assert "atlassian" in body["updated_sections"]
    assert password not in json.dumps(body)
    assert token not in json.dumps(body)
    assert config_path.exists()
    assert f"ATLASSIAN_CONFIG={config_path}" in (state / "opencode.env").read_text(encoding="utf-8")

    stored = json.loads(config_path.read_text(encoding="utf-8"))
    assert stored["jira"]["default_instance"] == "jira-main"
    assert stored["confluence"]["instances"][0]["default_space"] == "ENG"

    status = await (await client.get("/api/internal/runtime-profile/status")).json()
    assert status["atlassian_cli_configured"] is True
    assert status["atlassian_config_path"] == str(config_path)
    assert password not in json.dumps(status)
    assert token not in json.dumps(status)

    await client.close()
