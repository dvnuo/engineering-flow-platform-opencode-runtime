import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings


class FakeClient:
    async def health(self):
        return {"healthy": True, "version": "1.14.39"}


@pytest.mark.asyncio
async def test_effective_config_auth_present(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    data = tmp_path / "data"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(data))
    monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    (workspace / ".opencode").mkdir(parents=True, exist_ok=True)
    (workspace / ".opencode/opencode.json").write_text(json.dumps({"agent": {"efp-main": {"model": "github-copilot/gpt-x"}}, "provider": {"github-copilot": {"options": {"baseURL": "http://x"}}}}))
    data.mkdir(parents=True, exist_ok=True)
    (data / "auth.json").write_text(json.dumps({"github-copilot": {"type": "api", "key": "SECRET"}}))
    app = create_app(Settings.from_env(), opencode_client=FakeClient())
    c = TestClient(TestServer(app)); await c.start_server()
    body = await (await c.get('/api/internal/opencode-effective-config')).json()
    assert body['auth']['present'] is True
    assert 'SECRET' not in json.dumps(body)
    await c.close()
