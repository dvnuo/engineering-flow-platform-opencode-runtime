import os
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from efp_opencode_adapter import copilot_plugin_auth as mod
from efp_opencode_adapter.copilot_plugin_auth import (
    CopilotTokenManager,
    CopilotTokenExchangeError,
    copilot_plugin_auth_path,
    exchange_copilot_internal_token,
    extract_copilot_source_credential,
    load_copilot_plugin_credential,
    save_copilot_plugin_credential,
)
from efp_opencode_adapter.runtime_env import write_runtime_env_file
from efp_opencode_adapter.settings import Settings


def test_proxy_endpoint_parsing_supports_subscription_tier_hosts():
    assert mod.parse_copilot_api_base_url_from_token("tid=SECRET;proxy-ep=proxy.enterprise.githubcopilot.com") == "https://api.enterprise.githubcopilot.com"
    assert mod.parse_copilot_api_base_url_from_token("tid=SECRET;proxy-ep=proxy.business.githubcopilot.com") == "https://api.business.githubcopilot.com"
    assert mod.parse_copilot_api_base_url_from_token("tid=SECRET;proxy-ep=proxy.individual.githubcopilot.com") == "https://api.individual.githubcopilot.com"


def test_extracting_copilot_credential_prefers_oauth_refresh_then_access_and_api_key():
    refresh = extract_copilot_source_credential(
        {"llm": {"provider": "github_copilot", "oauth": {"refresh": " REFRESH ", "access": "ACCESS", "enterpriseUrl": " https://ghe.example.com "}, "api_key": "API"}}
    )
    assert refresh.credential == "REFRESH"
    assert refresh.source == "oauth_refresh"
    assert refresh.enterprise_url == "https://ghe.example.com"

    access = extract_copilot_source_credential({"llm": {"provider": "github_copilot", "oauth": {"access": " ACCESS "}, "api_key": "API"}})
    assert access.credential == "ACCESS"
    assert access.source == "oauth_access"

    api_key = extract_copilot_source_credential({"llm": {"provider": "github_copilot", "api_key": " API "}})
    assert api_key.credential == "API"
    assert api_key.source == "api_key"


def test_extracting_copilot_credential_ignores_oauth_by_runtime():
    result = extract_copilot_source_credential(
        {
            "llm": {
                "provider": "github_copilot",
                "oauth_by_runtime": {
                    "opencode": {"type": "oauth", "refresh": "OPENCODE_SECRET", "access": "OPENCODE_SECRET"},
                },
            }
        }
    )
    assert result is None


def test_copilot_credential_state_is_adapter_state_0600(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    settings = Settings.from_env()
    credential = extract_copilot_source_credential({"llm": {"provider": "github_copilot", "api_key": "gho_PORTAL"}})

    path = save_copilot_plugin_credential(settings, credential)

    assert path == copilot_plugin_auth_path(settings)
    assert str(path).startswith(str(settings.adapter_state_dir))
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    loaded = load_copilot_plugin_credential(settings)
    assert loaded.credential == "gho_PORTAL"


@pytest.mark.asyncio
async def test_exchange_request_includes_plugin_headers_and_parses_proxy_endpoint(tmp_path, monkeypatch):
    captured = {}

    async def token_handler(request):
        captured["method"] = request.method
        captured["headers"] = dict(request.headers)
        return web.json_response({"token": "tid=SECRET;proxy-ep=proxy.enterprise.githubcopilot.com", "expires_at": 4102444800})

    app = web.Application()
    app.router.add_get("/copilot_internal/v2/token", token_handler)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("EFP_COPILOT_GITHUB_API_BASE_URL", str(server.make_url("/")).rstrip("/"))

    settings = Settings.from_env()
    credential = extract_copilot_source_credential({"llm": {"provider": "github_copilot", "api_key": "gho_PORTAL"}})
    token = await exchange_copilot_internal_token(settings, credential)

    assert captured["method"] == "GET"
    assert captured["headers"]["Accept"] == "application/json"
    assert captured["headers"]["Authorization"] == "Bearer gho_PORTAL"
    assert captured["headers"]["User-Agent"] == "GitHubCopilotChat/0.35.0"
    assert captured["headers"]["Editor-Version"] == "vscode/1.107.0"
    assert captured["headers"]["Editor-Plugin-Version"] == "copilot-chat/0.35.0"
    assert captured["headers"]["Copilot-Integration-Id"] == "vscode-chat"
    assert token.token == "tid=SECRET;proxy-ep=proxy.enterprise.githubcopilot.com"
    assert token.api_base_url == "https://api.enterprise.githubcopilot.com"

    await server.close()


@pytest.mark.asyncio
async def test_exchange_uses_configured_copilot_api_fallback_when_token_has_no_proxy_endpoint(tmp_path, monkeypatch):
    async def token_handler(_request):
        return web.json_response({"token": "tid=SECRET", "expires_at": 4102444800})

    app = web.Application()
    app.router.add_get("/copilot_internal/v2/token", token_handler)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("EFP_COPILOT_GITHUB_API_BASE_URL", str(server.make_url("/")).rstrip("/"))
    monkeypatch.setenv("EFP_COPILOT_API_BASE_URL", "https://copilot-api.local/")

    settings = Settings.from_env()
    credential = extract_copilot_source_credential({"llm": {"provider": "github_copilot", "api_key": "gho_PORTAL"}})
    token = await exchange_copilot_internal_token(settings, credential)

    assert token.api_base_url == "https://copilot-api.local"

    await server.close()


@pytest.mark.asyncio
async def test_exchange_passes_selected_runtime_proxy_to_aiohttp(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HTTPS_PROXY", "http://process.proxy:8080")
    settings = Settings.from_env()
    write_runtime_env_file(settings, {"HTTPS_PROXY": "http://user:pass@runtime.proxy:8080"})
    captured = {}

    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def json(self):
            return {"token": "tid=SECRET;proxy-ep=proxy.enterprise.githubcopilot.com", "expires_at": 4102444800}

    class FakeSession:
        def __init__(self, **kwargs):
            captured["session_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        def get(self, url, **kwargs):
            captured["url"] = url
            captured["get_kwargs"] = kwargs
            return FakeResponse()

    monkeypatch.setattr(mod.aiohttp, "ClientSession", FakeSession)

    credential = extract_copilot_source_credential({"llm": {"provider": "github_copilot", "api_key": "gho_PORTAL"}})
    token = await exchange_copilot_internal_token(settings, credential)

    assert captured["url"] == "https://api.github.com/copilot_internal/v2/token"
    assert captured["session_kwargs"]["trust_env"] is True
    assert captured["get_kwargs"]["proxy"] == "http://user:pass@runtime.proxy:8080"
    assert token.api_base_url == "https://api.enterprise.githubcopilot.com"


@pytest.mark.asyncio
async def test_exchange_error_redacts_proxy_url_with_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    settings = Settings.from_env()
    proxy_url = "http://user:pass@runtime.proxy:8080"
    write_runtime_env_file(settings, {"HTTPS_PROXY": proxy_url})

    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        def get(self, _url, **_kwargs):
            raise RuntimeError(f"failed through {proxy_url}")

    monkeypatch.setattr(mod.aiohttp, "ClientSession", FakeSession)

    credential = extract_copilot_source_credential({"llm": {"provider": "github_copilot", "api_key": "gho_PORTAL"}})
    with pytest.raises(CopilotTokenExchangeError) as excinfo:
        await exchange_copilot_internal_token(settings, credential)

    detail = str(excinfo.value)
    assert proxy_url not in detail
    assert "user:pass" not in detail
    assert "runtime.proxy:8080" not in detail


@pytest.mark.asyncio
async def test_token_manager_uses_cache_until_near_expiry(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    now = [1_000_000]
    calls = {"count": 0}

    async def token_handler(_request):
        calls["count"] += 1
        return web.json_response({"token": f"tid=SECRET{calls['count']};proxy-ep=proxy.enterprise.githubcopilot.com", "expires_at": now[0] + 1000})

    app = web.Application()
    app.router.add_get("/copilot_internal/v2/token", token_handler)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("EFP_COPILOT_GITHUB_API_BASE_URL", str(server.make_url("/")).rstrip("/"))
    monkeypatch.setattr(mod.time, "time", lambda: now[0])

    settings = Settings.from_env()
    credential = extract_copilot_source_credential({"llm": {"provider": "github_copilot", "api_key": "gho_PORTAL"}})
    save_copilot_plugin_credential(settings, credential)
    manager = CopilotTokenManager(settings)

    first = await manager.get_token()
    second = await manager.get_token()
    assert first.token == second.token
    assert calls["count"] == 1

    now[0] += 701
    third = await manager.get_token()
    assert third.token != first.token
    assert calls["count"] == 2

    await server.close()


def test_source_does_not_use_individual_copilot_api_as_fallback_constant():
    source_dir = Path(__file__).resolve().parents[1] / "efp_opencode_adapter"
    matches = []
    for path in source_dir.rglob("*.py"):
        if "api.individual.githubcopilot.com" in path.read_text(encoding="utf-8"):
            matches.append(str(path.relative_to(source_dir.parent)))
    assert matches == []
