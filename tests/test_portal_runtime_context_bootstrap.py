import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

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
async def test_bootstrap_fetches_portal_context_and_writes_config_and_auth(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    data = tmp_path / "opdata"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
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
    assert cfg["provider"]["github-copilot"]["options"]["baseURL"] == "http://litellm.local/v1"

    auth = json.loads((data / "auth.json").read_text())
    assert auth == {"github-copilot": {"type": "oauth", "refresh": "gho_TEST", "access": "gho_TEST", "expires": 0}}

    emitted = capsys.readouterr()
    assert "gho_TEST" not in emitted.out
    assert "gho_TEST" not in emitted.err

    await server.close()


@pytest.mark.asyncio
async def test_bootstrap_copilot_ghu_token_skips_auth(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    data = tmp_path / "opdata"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(data))

    async def runtime_context(_):
        return web.json_response({"runtime_profile_context": {"config": {"llm": {"provider": "github_copilot", "api_key": "ghu_TEST"}}}})

    app = web.Application(); app.router.add_get("/api/internal/agents/agent-1/runtime-context", runtime_context)
    server = TestServer(app); await server.start_server()
    monkeypatch.setenv("PORTAL_INTERNAL_BASE_URL", str(server.make_url("/")).rstrip("/")); monkeypatch.setenv("PORTAL_AGENT_ID", "agent-1")
    assert await mod._run(workspace) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["auth_written"] is False
    assert "auth_warning" in out and "ghu_TEST" not in out["auth_warning"]
    if (data / "auth.json").exists():
        auth = json.loads((data / "auth.json").read_text())
        assert auth.get("github-copilot", {}).get("type") != "api"
    await server.close()


@pytest.mark.asyncio
async def test_bootstrap_infers_auth_provider_from_model_prefix(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    data = tmp_path / "opdata"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
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
    auth = json.loads((data / "auth.json").read_text())
    assert "github-copilot" in auth
    await server.close()
