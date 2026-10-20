"""Copilot "auto" through the adapter's loopback Copilot proxy.

OpenCode sends ``model: "auto"``; the proxy opens an auto session, rewrites the
model to the preferred GPT-6 id in the pool and adds Copilot-Session-Token, and
falls back to the default model without the session when it cannot.
"""
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter import copilot_auto_session as mod
from efp_opencode_adapter.app_keys import COPILOT_AUTO_SESSION_MANAGER_KEY, COPILOT_TOKEN_MANAGER_KEY, SETTINGS_KEY
from efp_opencode_adapter.copilot_auto_session import (
    AUTO_MODEL_ID,
    COPILOT_AUTO_MODEL_CONFIG,
    CopilotAutoSession,
    CopilotAutoSessionError,
    CopilotAutoSessionManager,
    is_auto_model,
    parse_auto_session,
    select_auto_model,
)
from efp_opencode_adapter.copilot_plugin_auth import CopilotInternalToken
from efp_opencode_adapter.copilot_proxy import copilot_proxy_handler
from efp_opencode_adapter.opencode_config import build_opencode_config
from efp_opencode_adapter.outbound_proxy import OutboundProxyConfig
from efp_opencode_adapter.settings import Settings

POOL = ["claude-sonnet-5.5", "gpt-6-luna", "gpt-6.1-sol", "gemini-3.8-flash", "gpt-6-sol"]


class StaticTokenManager:
    def __init__(self, api_base_url):
        self.token = CopilotInternalToken(token="tid=INTERNAL_TOKEN", expires_at=4102444800, api_base_url=api_base_url)

    async def get_token(self):
        return self.token

    def status_snapshot(self):
        return {"credential_present": True, "token_cached": True, "expires_at_present": True}


async def _upstream(pool=None, session_status=200):
    calls = {"session": 0, "chat": []}

    async def session_handler(request):
        calls["session"] += 1
        calls["session_headers"] = dict(request.headers)
        calls["session_body"] = await request.json()
        if session_status != 200:
            return web.Response(status=session_status, text="auto mode is not available for tid=INTERNAL_TOKEN")
        return web.json_response(
            {
                "session_token": "sess-123",
                "expires_at": 4102444800,
                "available_models": POOL if pool is None else pool,
                "discounted_costs": {"gpt-6.1-sol": 0.1},
            }
        )

    async def chat_handler(request):
        calls["chat"].append({"path": request.path, "headers": dict(request.headers), "body": await request.text()})
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/models/session", session_handler)
    app.router.add_route("*", "/{tail:.*}", chat_handler)
    server = TestServer(app)
    await server.start_server()
    return server, calls


def _proxy_client(settings, upstream_base):
    app = web.Application()
    app[SETTINGS_KEY] = settings
    app[COPILOT_TOKEN_MANAGER_KEY] = StaticTokenManager(upstream_base)
    app[COPILOT_AUTO_SESSION_MANAGER_KEY] = CopilotAutoSessionManager()
    app.router.add_route("*", "/api/internal/copilot/{tail:.*}", copilot_proxy_handler)
    return TestClient(TestServer(app))


@pytest.mark.asyncio
async def test_auto_request_is_sent_as_the_preferred_pool_model_with_the_session_token(tmp_path, monkeypatch):
    upstream, calls = await _upstream()
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    client = _proxy_client(Settings.from_env(), str(upstream.make_url("/")).rstrip("/"))
    await client.start_server()

    first = await client.post("/api/internal/copilot/responses", json={"model": "auto", "input": "hi", "stream": False})
    second = await client.post("/api/internal/copilot/responses", json={"model": "github-copilot/auto", "input": "again"})

    assert first.status == 200
    assert second.status == 200
    # One session per token while it is valid; both requests ride on it.
    assert calls["session"] == 1
    assert calls["session_headers"]["Authorization"] == "Bearer tid=INTERNAL_TOKEN"
    assert calls["session_headers"]["Copilot-Integration-Id"] == "vscode-chat"
    assert calls["session_headers"]["X-GitHub-Api-Version"] == mod.AUTO_SESSION_API_VERSION
    assert "Openai-Intent" not in calls["session_headers"]
    assert calls["session_body"] == {"auto_mode": {"model_hints": ["auto"]}}
    sent = [json.loads(call["body"]) for call in calls["chat"]]
    assert [body["model"] for body in sent] == ["gpt-6.1-sol", "gpt-6.1-sol"]
    assert sent[0]["input"] == "hi"
    assert sent[0]["stream"] is False
    assert [call["path"] for call in calls["chat"]] == ["/responses", "/responses"]
    assert all(call["headers"]["Copilot-Session-Token"] == "sess-123" for call in calls["chat"])
    assert all(call["headers"]["Authorization"] == "Bearer tid=INTERNAL_TOKEN" for call in calls["chat"])

    await client.close()
    await upstream.close()


@pytest.mark.asyncio
async def test_concrete_model_requests_pass_through_byte_for_byte(tmp_path, monkeypatch):
    upstream, calls = await _upstream()
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    client = _proxy_client(Settings.from_env(), str(upstream.make_url("/")).rstrip("/"))
    await client.start_server()

    body = b'{"model": "gpt-6-sol",  "input": "hi"}'
    response = await client.post("/api/internal/copilot/responses", data=body, headers={"Content-Type": "application/json"})
    not_json = await client.post("/api/internal/copilot/responses", data=b"model=auto")

    assert response.status == 200
    assert not_json.status == 200
    assert calls["session"] == 0
    assert calls["chat"][0]["body"] == body.decode("utf-8")
    assert calls["chat"][1]["body"] == "model=auto"
    assert all("Copilot-Session-Token" not in call["headers"] for call in calls["chat"])

    await client.close()
    await upstream.close()


@pytest.mark.asyncio
async def test_auto_falls_back_to_the_default_model_when_the_session_is_refused(tmp_path, monkeypatch, caplog):
    upstream, calls = await _upstream(session_status=404)
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    client = _proxy_client(Settings.from_env(), str(upstream.make_url("/")).rstrip("/"))
    await client.start_server()

    with caplog.at_level("WARNING"):
        first = await client.post("/api/internal/copilot/responses", json={"model": "auto", "input": "hi"})
        second = await client.post("/api/internal/copilot/responses", json={"model": "auto", "input": "again"})

    assert first.status == 200
    assert second.status == 200
    # Failures are not cached: every auto request tries the session again.
    assert calls["session"] == 2
    assert [json.loads(call["body"])["model"] for call in calls["chat"]] == ["gpt-6-sol", "gpt-6-sol"]
    assert all("Copilot-Session-Token" not in call["headers"] for call in calls["chat"])
    warnings = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "HTTP 404" in warnings[0]
    assert "INTERNAL_TOKEN" not in warnings[0]

    await client.close()
    await upstream.close()


@pytest.mark.asyncio
async def test_auto_falls_back_when_the_pool_has_no_supported_gpt_model(tmp_path, monkeypatch):
    upstream, calls = await _upstream(pool=["claude-opus-5.5", "gemini-3.8-flash", "gpt-5.3-codex"])
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    client = _proxy_client(Settings.from_env(), str(upstream.make_url("/")).rstrip("/"))
    await client.start_server()

    response = await client.post("/api/internal/copilot/responses", json={"model": "auto", "input": "hi"})

    assert response.status == 200
    assert json.loads(calls["chat"][0]["body"])["model"] == "gpt-6-sol"
    assert "Copilot-Session-Token" not in calls["chat"][0]["headers"]

    await client.close()
    await upstream.close()


def test_auto_detection_and_pool_selection():
    assert is_auto_model("auto")
    assert is_auto_model("github-copilot/Auto ")
    assert not is_auto_model("gpt-6-sol")
    assert not is_auto_model(None)
    assert select_auto_model(POOL) == "gpt-6.1-sol"
    assert select_auto_model(["github-copilot/gpt-6 luna", "claude-opus-5.5"]) == "gpt-6-luna"
    assert select_auto_model(["gpt-6-astra"]) == "gpt-6-astra"
    assert select_auto_model(["gpt-5.6-sol", "claude-opus-5.5"]) is None
    assert select_auto_model(None) is None


def test_parse_auto_session_validates_the_answer():
    session = parse_auto_session(
        {"session_token": " s ", "expires_at": 10.0, "available_models": ["gpt-6-sol", 3, " "], "discounted_costs": {"gpt-6-sol": 0.1}}
    )
    assert session == CopilotAutoSession("s", 10, ("gpt-6-sol",), {"gpt-6-sol": 0.1})
    for bad in ({}, {"session_token": "s"}, {"session_token": "s", "expires_at": True}, "nope"):
        with pytest.raises(CopilotAutoSessionError):
            parse_auto_session(bad)


@pytest.mark.asyncio
async def test_manager_reopens_the_session_inside_the_refresh_margin_and_per_token(monkeypatch):
    now = [1_000_000]
    opened = []
    manager = CopilotAutoSessionManager()

    async def fake_open(internal_token, proxy_config):
        opened.append(internal_token.token)
        return CopilotAutoSession(f"sess-{len(opened)}", now[0] + 1000, ("gpt-6-sol",), {})

    monkeypatch.setattr(manager, "_open", fake_open)
    monkeypatch.setattr(mod.time, "time", lambda: now[0])
    token = CopilotInternalToken(token="tid=A", expires_at=4102444800, api_base_url="https://api.example")
    proxy = OutboundProxyConfig(proxy_url=None)

    first = await manager.get_session(token, proxy_config=proxy)
    now[0] = first.expires_at - mod.AUTO_SESSION_REFRESH_MARGIN_SECONDS - 1
    assert await manager.get_session(token, proxy_config=proxy) is first
    now[0] = first.expires_at - mod.AUTO_SESSION_REFRESH_MARGIN_SECONDS + 1
    second = await manager.get_session(token, proxy_config=proxy)
    assert second.session_token == "sess-2"

    other = CopilotInternalToken(token="tid=B", expires_at=4102444800, api_base_url="https://api.example")
    third = await manager.get_session(other, proxy_config=proxy)
    assert third.session_token == "sess-3"
    assert opened == ["tid=A", "tid=A", "tid=B"]


def test_opencode_config_declares_the_auto_model_for_copilot(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))

    cfg, _, _ = build_opencode_config(
        Settings.from_env(),
        {"llm": {"provider": "github_copilot", "model": "auto", "api_key": "ghu_source"}},
    )

    assert cfg["agent"]["efp-main"]["model"] == "github-copilot/auto"
    provider_cfg = cfg["provider"]["github-copilot"]
    assert provider_cfg["npm"] == "@ai-sdk/openai"
    assert provider_cfg["models"] == {AUTO_MODEL_ID: COPILOT_AUTO_MODEL_CONFIG}
    assert provider_cfg["models"]["auto"]["limit"] == {"context": 1050000, "output": 128000}
