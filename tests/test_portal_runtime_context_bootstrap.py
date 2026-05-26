import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from efp_opencode_adapter.copilot_plugin_auth import copilot_plugin_auth_path
from efp_opencode_adapter import portal_runtime_context_bootstrap as mod


@pytest.mark.asyncio
async def test_env_missing_skips(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PORTAL_INTERNAL_BASE_URL", raising=False)
    monkeypatch.delenv("PORTAL_AGENT_ID", raising=False)
    monkeypatch.delenv("EFP_REQUIRE_PORTAL_RUNTIME_CONTEXT", raising=False)
    code = await mod._run(tmp_path)
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "skipped"


@pytest.mark.asyncio
async def test_bootstrap_requires_portal_context_env_when_required(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EFP_REQUIRE_PORTAL_RUNTIME_CONTEXT", "true")
    monkeypatch.delenv("PORTAL_INTERNAL_BASE_URL", raising=False)
    monkeypatch.delenv("PORTAL_AGENT_ID", raising=False)
    code = await mod._run(tmp_path)
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "error"


@pytest.mark.asyncio
async def test_bootstrap_fetches_portal_context_and_writes_proxy_config_and_plugin_credential(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    data = tmp_path / "opdata"
    state = tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(data))

    async def runtime_context(_):
        return web.json_response({
            "runtime_profile_context": {
                "runtime_profile_id": "rp-1",
                "revision": 3,
                "config": {
                    "llm": {
                        "provider": "github_copilot",
                        "model": "gpt-x",
                        "api_key": "gho_TEST",
                        "base_url": "http://litellm.local/v1",
                    }
                },
            }
        })

    app = web.Application()
    app.router.add_get("/api/internal/agents/agent-1/runtime-context", runtime_context)
    server = TestServer(app)
    await server.start_server()

    monkeypatch.setenv("PORTAL_INTERNAL_BASE_URL", str(server.make_url("/")).rstrip("/"))
    monkeypatch.setenv("PORTAL_AGENT_ID", "agent-1")
    code = await mod._run(workspace)
    assert code == 0

    cfg_path = workspace / ".opencode" / "opencode.json"
    assert cfg_path.exists()
    cfg = json.loads(cfg_path.read_text())
    assert cfg["agent"]["efp-main"]["model"] == "github-copilot/gpt-x"
    assert cfg["provider"]["github-copilot"]["options"]["baseURL"] == "http://127.0.0.1:8000/api/internal/copilot"
    assert "gho_TEST" not in cfg_path.read_text(encoding="utf-8")

    settings = mod.Settings.from_env()
    state_payload = json.loads(copilot_plugin_auth_path(settings).read_text(encoding="utf-8"))
    assert state_payload["credential"] == "gho_TEST"
    assert not (data / "auth.json").exists()

    emitted = capsys.readouterr()
    assert "gho_TEST" not in emitted.out
    assert "gho_TEST" not in emitted.err

    await server.close()


@pytest.mark.asyncio
async def test_bootstrap_copilot_ghu_token_writes_plugin_credential_not_auth(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    data = tmp_path / "opdata"
    state = tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(data))

    async def runtime_context(_):
        return web.json_response({"runtime_profile_context": {"config": {"llm": {"provider": "github_copilot", "api_key": "ghu_TEST"}}}})

    app = web.Application(); app.router.add_get("/api/internal/agents/agent-1/runtime-context", runtime_context)
    server = TestServer(app); await server.start_server()
    monkeypatch.setenv("PORTAL_INTERNAL_BASE_URL", str(server.make_url("/")).rstrip("/")); monkeypatch.setenv("PORTAL_AGENT_ID", "agent-1")
    assert await mod._run(workspace) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["auth_written"] is False
    assert out["copilot_credential_present"] is True
    assert "auth_warning" not in out
    settings = mod.Settings.from_env()
    state_payload = json.loads(copilot_plugin_auth_path(settings).read_text(encoding="utf-8"))
    assert state_payload["credential"] == "ghu_TEST"
    assert not (data / "auth.json").exists()
    await server.close()


@pytest.mark.asyncio
async def test_bootstrap_infers_auth_provider_from_model_prefix(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    data = tmp_path / "opdata"
    state = tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(data))

    async def runtime_context(_):
        return web.json_response({
            "runtime_profile_context": {
                "runtime_profile_id": "rp-1",
                "revision": 3,
                "config": {"llm": {"model": "github_copilot/gpt-x", "api_key": "gho_TEST"}},
            }
        })

    app = web.Application()
    app.router.add_get("/api/internal/agents/agent-1/runtime-context", runtime_context)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("PORTAL_INTERNAL_BASE_URL", str(server.make_url("/")).rstrip("/"))
    monkeypatch.setenv("PORTAL_AGENT_ID", "agent-1")

    assert await mod._run(workspace) == 0
    settings = mod.Settings.from_env()
    state_payload = json.loads(copilot_plugin_auth_path(settings).read_text(encoding="utf-8"))
    assert state_payload["credential"] == "gho_TEST"
    cfg = json.loads((workspace / ".opencode" / "opencode.json").read_text(encoding="utf-8"))
    assert cfg["provider"]["github-copilot"]["npm"] == "@ai-sdk/openai"
    assert cfg["provider"]["github-copilot"]["options"]["baseURL"] == "http://127.0.0.1:8000/api/internal/copilot"
    assert cfg["provider"]["github-copilot"]["options"]["apiKey"] == "efp-copilot-proxy"
    assert not (data / "auth.json").exists()
    await server.close()


@pytest.mark.asyncio
async def test_bootstrap_oauth_by_runtime_is_ignored_without_leaking_tokens(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    data = tmp_path / "opdata"
    state = tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(data))

    async def runtime_context(_):
        return web.json_response({
            "runtime_profile_context": {
                "config": {
                    "llm": {
                        "provider": "github_copilot",
                        "oauth_by_runtime": {
                            "native": {"type": "oauth", "access": "NATIVE_SECRET", "refresh": "NATIVE_SECRET", "expires": 0},
                            "opencode": {"type": "oauth", "access": "OPENCODE_SECRET", "refresh": "OPENCODE_SECRET", "expires": 0},
                        },
                    }
                }
            }
        })

    app = web.Application(); app.router.add_get("/api/internal/agents/agent-1/runtime-context", runtime_context)
    server = TestServer(app); await server.start_server()

    monkeypatch.setenv("PORTAL_INTERNAL_BASE_URL", str(server.make_url("/")).rstrip("/"))
    monkeypatch.setenv("PORTAL_AGENT_ID", "agent-1")

    assert await mod._run(workspace) == 0
    settings = mod.Settings.from_env()
    assert not copilot_plugin_auth_path(settings).exists()
    assert not (data / "auth.json").exists()

    emitted = capsys.readouterr()
    assert "OPENCODE_SECRET" not in emitted.out
    assert "OPENCODE_SECRET" not in emitted.err
    assert "NATIVE_SECRET" not in emitted.out
    assert "NATIVE_SECRET" not in emitted.err

    await server.close()
