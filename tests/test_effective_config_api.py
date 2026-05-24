import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.copilot_plugin_auth import CopilotSourceCredential, save_copilot_plugin_credential
from efp_opencode_adapter.profile_store import ProfileOverlay, ProfileOverlayStore
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings


class FakeClient:
    async def health(self):
        return {"healthy": True, "version": "1.14.39"}


@pytest.mark.asyncio
async def test_effective_config_profile_and_copilot_integration_are_safe(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    data = tmp_path / "data"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(data))
    monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    monkeypatch.setenv("EFP_COPILOT_API_BASE_URL", "https://fallback.copilot-api.local/")
    (workspace / ".opencode").mkdir(parents=True, exist_ok=True)
    (workspace / ".opencode/opencode.json").write_text(json.dumps({"agent": {"efp-main": {"model": "github-copilot/gpt-x"}}, "provider": {"github-copilot": {"options": {"baseURL": "http://127.0.0.1:8000/api/internal/copilot"}}}}))
    data.mkdir(parents=True, exist_ok=True)
    (data / "auth.json").write_text(json.dumps({"github-copilot": {"type": "oauth", "refresh": "SECRET", "access": "SECRET"}}))
    settings = Settings.from_env()
    save_copilot_plugin_credential(settings, CopilotSourceCredential(credential="gho_SECRET", source="api_key"))
    ProfileOverlayStore(settings).save(ProfileOverlay(runtime_profile_id="rp-1", revision=3, config={}, applied_at="2026-01-01T00:00:00Z", generated_config_hash="h", status="applied", pending_restart=False, warnings=[], updated_sections=["llm"], last_apply_error=None, applied=True))

    app = create_app(settings, opencode_client=FakeClient())
    c = TestClient(TestServer(app)); await c.start_server()
    body = await (await c.get('/api/internal/opencode-effective-config')).json()
    assert body['auth']['present'] is False
    assert body['profile']['runtime_profile_id'] == 'rp-1'
    assert body['profile']['revision'] == 3
    assert 'SECRET' not in json.dumps(body)
    assert "fallback.copilot-api.local" not in json.dumps(body)
    assert "external_tools" not in body
    assert "runtime_integrations" in body
    copilot = body["runtime_integrations"]["copilot"]
    assert copilot == {
        "enabled": True,
        "credential_present": True,
        "token_cached": False,
        "base_url_present": True,
        "expires_at_present": False,
    }
    await c.close()


@pytest.mark.asyncio
async def test_effective_config_does_not_expose_external_tools_key(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    data = tmp_path / "data"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(data))
    monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    (workspace / ".opencode").mkdir(parents=True, exist_ok=True)
    (workspace / ".opencode/opencode.json").write_text(json.dumps({"agent": {"efp-main": {"model": "github-copilot/gpt-x"}}}), encoding="utf-8")
    settings = Settings.from_env()
    ProfileOverlayStore(settings).save(ProfileOverlay(runtime_profile_id="rp-2", revision=4, config={"github": {"api_token": "SECRET"}, "proxy": {"enabled": True, "password": "SECRET", "url": "http://proxy.local"}}, applied_at="2026-01-01T00:00:00Z", generated_config_hash="h2", status="applied", pending_restart=False, warnings=[], updated_sections=["llm"], last_apply_error=None, applied=True, env_path="/tmp/runtime.env", env_hash="h3"))

    app = create_app(settings, opencode_client=FakeClient())
    c = TestClient(TestServer(app)); await c.start_server()
    body = await (await c.get("/api/internal/opencode-effective-config")).json()
    assert "external_tools" not in body
    assert "runtime_integrations" in body
    assert set(body["runtime_integrations"].keys()) == {"github", "copilot", "proxy", "env_file"}
    assert body["runtime_integrations"]["github"]["enabled"] is True
    assert set(body["runtime_integrations"]["copilot"].keys()) == {"enabled", "credential_present", "token_cached", "base_url_present", "expires_at_present"}
    assert all(isinstance(value, bool) for value in body["runtime_integrations"]["copilot"].values())
    assert body["runtime_integrations"]["proxy"]["enabled"] is True
    assert body["runtime_integrations"]["env_file"]["present"] is True
    assert "SECRET" not in json.dumps(body)
    await c.close()
