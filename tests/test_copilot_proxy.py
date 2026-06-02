import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.app_keys import COPILOT_TOKEN_MANAGER_KEY, SETTINGS_KEY
from efp_opencode_adapter import copilot_proxy as mod
from efp_opencode_adapter.copilot_plugin_auth import CopilotCredentialMissing, CopilotInternalToken, CopilotTokenExchangeError
from efp_opencode_adapter.copilot_proxy import copilot_proxy_handler
from efp_opencode_adapter.runtime_env import write_runtime_env_file
from efp_opencode_adapter.settings import Settings


class StaticTokenManager:
    def __init__(self, api_base_url):
        self.token = CopilotInternalToken(token="tid=INTERNAL_TOKEN", expires_at=4102444800, api_base_url=api_base_url)

    async def get_token(self):
        return self.token

    def status_snapshot(self):
        return {"credential_present": True, "token_cached": True, "expires_at_present": True}


class MissingTokenManager:
    async def get_token(self):
        raise CopilotCredentialMissing("missing")

    def status_snapshot(self):
        return {"credential_present": False, "token_cached": False, "expires_at_present": False}


class ErrorTokenManager:
    async def get_token(self):
        raise CopilotTokenExchangeError("Authorization: Bearer ghu_SOURCE gho_OTHER tid=INTERNAL")


def _proxy_app(settings, manager):
    app = web.Application()
    app[SETTINGS_KEY] = settings
    app[COPILOT_TOKEN_MANAGER_KEY] = manager
    app.router.add_route("*", "/api/internal/copilot/{tail:.*}", copilot_proxy_handler)
    return app


@pytest.mark.asyncio
async def test_non_loopback_request_gets_403(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = _proxy_app(Settings.from_env(), MissingTokenManager())
    client = TestClient(TestServer(app))
    await client.start_server()

    response = await client.post("/api/internal/copilot/chat/completions", headers={"X-Forwarded-For": "203.0.113.10"})

    assert response.status == 403
    await client.close()


@pytest.mark.asyncio
async def test_no_credential_gets_401(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = _proxy_app(Settings.from_env(), MissingTokenManager())
    client = TestClient(TestServer(app))
    await client.start_server()

    response = await client.post("/api/internal/copilot/chat/completions")
    body = await response.json()

    assert response.status == 401
    assert body["error"] == "copilot credential not configured"
    await client.close()


@pytest.mark.asyncio
async def test_proxy_forwards_chat_and_responses_to_upstream_with_internal_token(tmp_path, monkeypatch):
    captured = []

    async def upstream_handler(request):
        captured.append({"path_qs": request.path_qs, "headers": dict(request.headers), "body": await request.text()})
        return web.json_response({"ok": True})

    upstream_app = web.Application()
    upstream_app.router.add_route("*", "/{tail:.*}", upstream_handler)
    upstream = TestServer(upstream_app)
    await upstream.start_server()

    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = _proxy_app(Settings.from_env(), StaticTokenManager(str(upstream.make_url("/")).rstrip("/")))
    client = TestClient(TestServer(app))
    await client.start_server()

    chat_response = await client.post(
        "/api/internal/copilot/chat/completions?trace=1",
        headers={"Authorization": "Bearer ghu_SHOULD_NOT_FORWARD", "X-Api-Key": "SECRET", "Accept": "application/json"},
        json={"messages": []},
    )
    responses_response = await client.post("/api/internal/copilot/responses", data=b"{}")

    assert chat_response.status == 200
    assert responses_response.status == 200
    assert captured[0]["path_qs"] == "/chat/completions?trace=1"
    assert captured[1]["path_qs"] == "/responses"
    assert captured[0]["headers"]["Authorization"] == "Bearer tid=INTERNAL_TOKEN"
    assert captured[0]["headers"]["User-Agent"] == "GitHubCopilotChat/0.35.0"
    assert captured[0]["headers"]["Editor-Version"] == "vscode/1.107.0"
    assert captured[0]["headers"]["Editor-Plugin-Version"] == "copilot-chat/0.35.0"
    assert captured[0]["headers"]["Copilot-Integration-Id"] == "vscode-chat"
    assert captured[0]["headers"]["Openai-Intent"] == "conversation-edits"
    assert captured[0]["headers"]["x-initiator"] == "agent"
    encoded = json.dumps(captured)
    assert "ghu_SHOULD_NOT_FORWARD" not in encoded
    assert "X-Api-Key" not in encoded

    await client.close()
    await upstream.close()


@pytest.mark.asyncio
async def test_sse_response_is_streamed(tmp_path, monkeypatch):
    async def sse_handler(request):
        response = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b"data: one\n\n")
        await asyncio.sleep(0)
        await response.write(b"data: two\n\n")
        await response.write_eof()
        return response

    upstream_app = web.Application()
    upstream_app.router.add_post("/chat/completions", sse_handler)
    upstream = TestServer(upstream_app)
    await upstream.start_server()

    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = _proxy_app(Settings.from_env(), StaticTokenManager(str(upstream.make_url("/")).rstrip("/")))
    client = TestClient(TestServer(app))
    await client.start_server()

    response = await client.post("/api/internal/copilot/chat/completions")
    text = await response.text()

    assert response.status == 200
    assert "text/event-stream" in response.headers["Content-Type"]
    assert text == "data: one\n\ndata: two\n\n"

    await client.close()
    await upstream.close()


@pytest.mark.asyncio
async def test_proxy_passes_selected_runtime_proxy_to_aiohttp(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    settings = Settings.from_env()
    write_runtime_env_file(settings, {"HTTPS_PROXY": "http://user:pass@runtime.proxy:8080"})
    captured = {}

    class FakeUpstreamResponse:
        status = 200
        headers = {"Content-Type": "application/json"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def read(self):
            return b'{"ok":true}'

    class FakeSession:
        def __init__(self, **kwargs):
            captured["session_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        def request(self, method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            captured["request_kwargs"] = kwargs
            return FakeUpstreamResponse()

    monkeypatch.setattr(mod.aiohttp, "ClientSession", FakeSession)
    app = _proxy_app(settings, StaticTokenManager("https://api.enterprise.githubcopilot.com"))
    client = TestClient(TestServer(app))
    await client.start_server()

    response = await client.post("/api/internal/copilot/chat/completions", data=b"{}")

    assert response.status == 200
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.enterprise.githubcopilot.com/chat/completions"
    assert captured["session_kwargs"]["trust_env"] is True
    assert captured["session_kwargs"]["timeout"].total is None
    assert captured["request_kwargs"]["proxy"] == "http://user:pass@runtime.proxy:8080"

    await client.close()


@pytest.mark.asyncio
async def test_proxy_upstream_error_redacts_proxy_url_with_credentials(tmp_path, monkeypatch):
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

        def request(self, _method, _url, **_kwargs):
            raise RuntimeError(f"failed through {proxy_url}")

    monkeypatch.setattr(mod.aiohttp, "ClientSession", FakeSession)
    app = _proxy_app(settings, StaticTokenManager("https://api.enterprise.githubcopilot.com"))
    client = TestClient(TestServer(app))
    await client.start_server()

    response = await client.post("/api/internal/copilot/chat/completions", data=b"{}")
    text = await response.text()

    assert response.status == 502
    assert proxy_url not in text
    assert "user:pass" not in text
    assert "runtime.proxy:8080" not in text

    await client.close()


@pytest.mark.asyncio
async def test_proxy_errors_are_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = _proxy_app(Settings.from_env(), ErrorTokenManager())
    client = TestClient(TestServer(app))
    await client.start_server()

    response = await client.post("/api/internal/copilot/chat/completions")
    text = await response.text()

    assert response.status == 502
    assert "ghu_" not in text
    assert "gho_" not in text
    assert "tid=" not in text
    assert "Authorization" not in text

    await client.close()
