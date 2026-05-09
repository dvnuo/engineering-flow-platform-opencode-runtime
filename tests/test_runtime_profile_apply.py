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
        return {"healthy": True, "version": "1.14.39"}

    async def put_auth_info(self, provider, auth_info):
        self.auth_calls.append((provider, auth_info))
        return {"success": self.auth_success, "status": 500 if not self.auth_success else 200}

    async def put_auth(self, provider, api_key):
        return await self.put_auth_info(provider, {"type": "api", "key": api_key})

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


class RaisingOpenCodeClient:
    async def health(self):
        return {"healthy": True, "version": "1.14.39"}

    async def put_auth_info(self, provider, auth_info):
        raise RuntimeError("boom SECRET-KEY-SHOULD-NOT-LEAK")

    async def patch_config(self, config):
        raise RuntimeError("patch boom SECRET-KEY-SHOULD-NOT-LEAK")


@pytest.mark.asyncio
async def test_apply_client_exceptions_are_best_effort_and_sanitized(tmp_path, monkeypatch):
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    app = create_app(Settings.from_env(), opencode_client=RaisingOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    secret = "SECRET-KEY-SHOULD-NOT-LEAK"
    resp = await client.post("/api/internal/runtime-profile/apply", headers={"X-Portal-Author-Source": "portal"}, json={"runtime_profile_id": "rp1", "revision": 1, "config": {"llm": {"provider": "anthropic", "model": "claude", "api_key": secret}}})
    body = await resp.json()
    assert resp.status == 200
    assert body["success"] is True
    assert any("auth update failed" in w for w in body["warnings"])
    assert body["pending_restart"] is True
    encoded = json.dumps(body)
    assert secret not in encoded
    assert (workspace / ".opencode/opencode.json").exists()
    assert secret not in (workspace / ".opencode/opencode.json").read_text()
    await client.close()


class FakeAuthOnlyClient:
    def __init__(self):
        self.auth_calls = []

    async def health(self):
        return {"healthy": True, "version": "1.14.39"}

    async def put_auth_info(self, provider, auth_info):
        self.auth_calls.append((provider, auth_info))
        return {"success": True, "status": 200}

    async def put_auth(self, provider, api_key):
        return await self.put_auth_info(provider, {"type": "api", "key": api_key})

    async def patch_config(self, config):
        return {"success": True, "status": 200}


@pytest.mark.asyncio
async def test_apply_auth_only_llm_marks_llm_updated(tmp_path, monkeypatch):
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))

    fake = FakeAuthOnlyClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()

    secret = "SECRET-KEY-SHOULD-NOT-LEAK"
    resp = await client.post(
        "/api/internal/runtime-profile/apply",
        headers={"X-Portal-Author-Source": "portal"},
        json={
            "runtime_profile_id": "rp-auth",
            "revision": 2,
            "config": {"llm": {"provider": "anthropic", "api_key": secret}},
        },
    )
    body = await resp.json()

    assert resp.status == 200
    assert body["success"] is True
    assert "llm" in body["updated_sections"]
    assert "permission" in body["updated_sections"]
    assert "agent" in body["updated_sections"]
    assert secret not in json.dumps(body)

    cfg_text = (workspace / ".opencode/opencode.json").read_text()
    assert secret not in cfg_text
    assert fake.auth_calls == [("anthropic", {"type": "api", "key": secret})]

    await client.close()


class PendingRestartClient(FakeAuthOnlyClient):
    async def patch_config(self, config):
        return {"success": False, "pending_restart": True, "status": 404}


class AuthFailurePatchSuccessClient(FakeAuthOnlyClient):
    async def put_auth_info(self, provider, auth_info):
        return {"success": False, "status": 500}


@pytest.mark.asyncio
async def test_profile_status_endpoint_applied(tmp_path, monkeypatch):
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace)); monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state)); monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    app = create_app(Settings.from_env(), opencode_client=FakeAuthOnlyClient()); c = TestClient(TestServer(app)); await c.start_server()
    body = await (await c.post("/api/internal/runtime-profile/apply", headers={"X-Portal-Author-Source": "portal"}, json={"config": {"allowed_skills": []}})).json()
    assert body["status"] == "applied" and body["applied"] is True and body["pending_restart"] is False
    st = await (await c.get("/api/internal/runtime-profile/status")).json()
    assert st["status"] == "applied" and st["applied"] is True and st["pending_restart"] is False
    await c.close()


@pytest.mark.asyncio
async def test_profile_status_endpoint_pending_restart(tmp_path, monkeypatch):
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace)); monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state)); monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    app = create_app(Settings.from_env(), opencode_client=PendingRestartClient()); c = TestClient(TestServer(app)); await c.start_server()
    body = await (await c.post("/api/internal/runtime-profile/apply", headers={"X-Portal-Author-Source": "portal"}, json={"config": {"allowed_skills": []}})).json()
    assert body["status"] == "pending_restart"
    st = await (await c.get("/api/internal/runtime-profile/status")).json()
    assert st["status"] == "pending_restart" and st["restart_required"] is True
    await c.close()


@pytest.mark.asyncio
async def test_profile_auth_failure_status_partially_applied(tmp_path, monkeypatch):
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace)); monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state)); monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    app = create_app(Settings.from_env(), opencode_client=AuthFailurePatchSuccessClient()); c = TestClient(TestServer(app)); await c.start_server()
    body = await (await c.post("/api/internal/runtime-profile/apply", headers={"X-Portal-Author-Source": "portal"}, json={"config": {"llm": {"provider": "anthropic", "api_key": "SECRET-KEY-SHOULD-NOT-LEAK"}}})).json()
    assert body["status"] == "partially_applied" and body["auth_update_status"] == "failed"
    assert any("auth update failed" in x for x in body["warnings"])
    st = await (await c.get("/api/internal/runtime-profile/status")).json()
    assert st["status"] == "partially_applied"
    await c.close()


@pytest.mark.asyncio
async def test_apply_allowed_skills_reflects_capability_state(tmp_path, monkeypatch):
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    state.mkdir(parents=True)
    (state / "skills-index.json").write_text(json.dumps({"skills": [{"opencode_name": "my-skill"}]}))
    app = create_app(Settings.from_env(), opencode_client=FakeAuthOnlyClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    resp = await client.post("/api/internal/runtime-profile/apply", headers={"X-Portal-Author-Source": "portal"}, json={"config": {"allowed_skills": ["my-skill"]}})
    assert resp.status == 200
    cfg = json.loads((workspace / ".opencode/opencode.json").read_text())
    assert cfg["permission"]["skill"]["my-skill"] == "allow"
    caps = await (await client.get("/api/capabilities")).json()
    skill = next(c for c in caps["capabilities"] if c.get("type") == "skill" and c.get("name") == "my-skill")
    assert skill["permission_state"] == "allowed"
    await client.close()

@pytest.mark.asyncio
async def test_apply_github_copilot_oauth_uses_put_auth_info(tmp_path, monkeypatch):
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace)); monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state)); monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    fake = FakeAuthOnlyClient()
    app = create_app(Settings.from_env(), opencode_client=fake); c = TestClient(TestServer(app)); await c.start_server()
    resp = await c.post("/api/internal/runtime-profile/apply", headers={"X-Portal-Author-Source": "portal"}, json={"config": {"llm": {"provider": "github_copilot", "model": "gpt", "oauth": {"type": "oauth", "refresh": "gho_R", "access": "gho_A", "expires": 0}}}})
    body = await resp.json()
    assert body["auth_update_status"] == "updated"
    assert fake.auth_calls[0][0] == "github-copilot"
    assert fake.auth_calls[0][1]["type"] == "oauth"
    await c.close()


@pytest.mark.asyncio
async def test_apply_github_copilot_ghu_skips_auth(tmp_path, monkeypatch):
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace)); monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state)); monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    fake = FakeAuthOnlyClient()
    app = create_app(Settings.from_env(), opencode_client=fake); c = TestClient(TestServer(app)); await c.start_server()
    resp = await c.post("/api/internal/runtime-profile/apply", headers={"X-Portal-Author-Source": "portal"}, json={"config": {"llm": {"provider": "github_copilot", "api_key": "ghu_TEST"}}})
    body = await resp.json()
    assert body["auth_update_status"] == "skipped"
    assert body.get("auth_warning")
    assert "ghu_TEST" not in json.dumps(body)
    assert fake.auth_calls == []
    await c.close()


@pytest.mark.asyncio
async def test_apply_github_copilot_oauth_by_runtime_uses_opencode_entry(tmp_path, monkeypatch):
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace)); monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state)); monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    fake = FakeAuthOnlyClient()
    app = create_app(Settings.from_env(), opencode_client=fake); c = TestClient(TestServer(app)); await c.start_server()
    resp = await c.post(
        "/api/internal/runtime-profile/apply",
        headers={"X-Portal-Author-Source": "portal"},
        json={
            "config": {
                "llm": {
                    "provider": "github_copilot",
                    "oauth_by_runtime": {
                        "native": {"type": "oauth", "access": "NATIVE_SECRET", "refresh": "NATIVE_SECRET", "expires": 0},
                        "opencode": {"type": "oauth", "access": "OPENCODE_SECRET", "refresh": "OPENCODE_SECRET", "expires": 0},
                    },
                }
            }
        },
    )
    body = await resp.json()
    assert resp.status == 200
    assert "llm" in body["updated_sections"]
    assert fake.auth_calls == [(
        "github-copilot",
        {"type": "oauth", "access": "OPENCODE_SECRET", "refresh": "OPENCODE_SECRET", "expires": 0},
    )]
    encoded = json.dumps(body)
    assert "NATIVE_SECRET" not in encoded
    assert "OPENCODE_SECRET" not in encoded
    await c.close()
