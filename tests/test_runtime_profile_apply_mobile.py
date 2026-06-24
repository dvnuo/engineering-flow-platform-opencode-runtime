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
async def test_runtime_profile_apply_writes_mobile_config_and_env(tmp_path, monkeypatch):
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    monkeypatch.setenv("EFP_CONFIG", str(workspace / ".efp/config.yaml"))

    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    access_key = "bs-access-key-secret"
    resp = await client.post(
        "/api/internal/runtime-profile/apply",
        headers={"X-Portal-Author-Source": "portal"},
        json={
            "runtime_profile_id": "rp-mobile",
            "revision": 2,
            "config": {
                "mobile": {
                    "enabled": True,
                    "default_provider": "browserstack",
                    "defaults": {"platform": "android", "network_mode": "private-external"},
                    "browserstack": {
                        "username": "bs-user",
                        "access_key": access_key,
                        "api_base_url": "https://api.browserstack.com",
                        "appium_base_url": "https://hub-cloud.browserstack.com/wd/hub",
                        "local": {"mode": "external"},
                    },
                }
            },
        },
    )
    body = await resp.json()

    assert resp.status == 200
    assert body["mobile_cli_configured"] is True
    assert body["mobile_config_path"] == str(workspace / ".efp/config.yaml")
    assert body["mobile_status"]["browserstack"]["access_key_present"] is True
    assert "mobile" in body["updated_sections"]
    assert access_key not in json.dumps(body)

    stored = yaml.safe_load((workspace / ".efp/config.yaml").read_text(encoding="utf-8"))
    assert stored["mobile"]["browserstack"]["username"] == "bs-user"
    assert stored["mobile"]["browserstack"]["access_key"] == access_key
    assert stored["mobile"]["browserstack"]["local"]["binary"] == "/usr/local/bin/BrowserStackLocal"

    env_text = (state / "opencode.env").read_text(encoding="utf-8")
    assert "EFP_CONFIG=" in env_text
    assert "MOBILE_STATE_DIR=" in env_text
    assert "BROWSERSTACK_LOCAL_BINARY=" in env_text

    status = await (await client.get("/api/internal/runtime-profile/status")).json()
    assert status["mobile_cli_configured"] is True
    assert status["mobile_config_path"] == str(workspace / ".efp/config.yaml")
    assert access_key not in json.dumps(status)

    effective = await (await client.get("/api/internal/opencode-effective-config")).json()
    assert effective["runtime_integrations"]["mobile"]["enabled"] is True
    assert effective["runtime_integrations"]["mobile"]["cli_configured"] is True
    assert effective["runtime_integrations"]["mobile"]["browserstack_username_present"] is True
    assert effective["runtime_integrations"]["mobile"]["browserstack_access_key_present"] is True

    await client.close()
