import json

import pytest
import yaml
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
    # atlassian config is now merged into the shared EFP config file (the path
    # EFP_CONFIG points to), not a separate JSON, so the CLI resolves it.
    efp_config_path = workspace / ".efp" / "config.yaml"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    monkeypatch.delenv("ATLASSIAN_CONFIG", raising=False)
    monkeypatch.delenv("EFP_CONFIG", raising=False)

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
    assert body["atlassian_config_path"] == str(efp_config_path)
    assert body["atlassian_jira_instances"] == 1
    assert body["atlassian_confluence_instances"] == 1
    assert "atlassian" in body["updated_sections"]
    assert password not in json.dumps(body)
    assert token not in json.dumps(body)
    assert efp_config_path.exists()
    env_text = (state / "opencode.env").read_text(encoding="utf-8")
    assert "ATLASSIAN_CONFIG=" in env_text
    assert str(efp_config_path) in env_text

    stored = yaml.safe_load(efp_config_path.read_text(encoding="utf-8"))
    assert stored["jira"]["default_instance"] == "jira-main"
    assert stored["confluence"]["instances"][0]["default_space"] == "ENG"

    status = await (await client.get("/api/internal/runtime-profile/status")).json()
    assert status["atlassian_cli_configured"] is True
    assert status["atlassian_config_path"] == str(efp_config_path)
    assert password not in json.dumps(status)
    assert token not in json.dumps(status)

    await client.close()
