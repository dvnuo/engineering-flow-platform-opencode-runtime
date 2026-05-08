import asyncio
import json

import pytest
import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from efp_opencode_adapter.opencode_client import OpenCodeClient
from efp_opencode_adapter.opencode_client import OpenCodeClientError
from efp_opencode_adapter.opencode_client import _safe_error_preview
from efp_opencode_adapter.settings import Settings


def server_base_url(server: TestServer) -> str:
    return str(server.make_url("")).rstrip("/")


@pytest.mark.asyncio
async def test_health_and_wait_ready(monkeypatch):
    app = web.Application()

    async def h(_):
        return web.json_response({"healthy": True, "version": "1.14.39"})

    app.router.add_get("/global/health", h)
    server = TestServer(app)
    await server.start_server()

    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server))
    settings = Settings.from_env()
    client = OpenCodeClient(settings)
    health = await client.health()
    assert health["healthy"] is True
    assert health["version"] == "1.14.39"
    await client.wait_until_ready(timeout_seconds=1)
    await server.close()


@pytest.mark.asyncio
async def test_health_does_not_send_authorization_header(monkeypatch):
    app = web.Application()

    async def h(request: web.Request):
        assert request.headers.get("Authorization") is None
        return web.json_response({"healthy": True, "version": "1.14.29"})

    app.router.add_get("/global/health", h)
    server = TestServer(app)
    await server.start_server()

    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server))
    monkeypatch.setenv("OPENCODE_VERSION", "1.14.39")
    settings = Settings.from_env()
    client = OpenCodeClient(settings)
    await client.wait_until_ready(timeout_seconds=1)
    await server.close()


@pytest.mark.asyncio
async def test_put_auth_sends_opencode_api_auth_payload_without_basic_auth(monkeypatch):
    app = web.Application()

    async def put_auth(request: web.Request):
        assert request.headers.get("Authorization") is None
        body = await request.json()
        assert body == {"type": "api", "key": "secret-value"}
        return web.json_response({}, status=200)

    app.router.add_put("/auth/anthropic", put_auth)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server))
    result = await OpenCodeClient(Settings.from_env()).put_auth("anthropic", "secret-value")
    assert result["success"] is True
    await server.close()


@pytest.mark.asyncio
async def test_unreachable_health_is_unhealthy(monkeypatch):
    monkeypatch.setenv("EFP_OPENCODE_URL", "http://127.0.0.1:9")
    settings = Settings.from_env()
    client = OpenCodeClient(settings)
    health = await client.health()
    assert health["healthy"] is False
    assert "error" in health


@pytest.mark.asyncio
async def test_wait_ready_ignores_version_mismatch(monkeypatch):
    app = web.Application()

    async def h(_):
        return web.json_response({"healthy": True, "version": "9.9.9"})

    app.router.add_get("/global/health", h)
    server = TestServer(app)
    await server.start_server()

    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server))
    monkeypatch.setenv("OPENCODE_VERSION", "1.14.29")
    settings = Settings.from_env()
    client = OpenCodeClient(settings)
    await client.wait_until_ready(timeout_seconds=1)
    await server.close()


@pytest.mark.asyncio
async def test_prompt_async_accepts_204(monkeypatch):
    app = web.Application()
    captured = {}

    async def prompt(request: web.Request):
        captured["body"] = await request.json()
        return web.Response(status=204)

    app.router.add_post("/session/ses-1/prompt_async", prompt)
    server = TestServer(app)
    await server.start_server()

    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server))
    client = OpenCodeClient(Settings.from_env())
    payload = {"parts": [{"type": "text", "text": "hi"}], "model": "anthropic/claude-sonnet-4", "agent": "efp"}
    result = await client.prompt_async("ses-1", payload)
    assert result is None
    assert payload["model"] == "anthropic/claude-sonnet-4"
    assert captured["body"]["model"] == {"providerID": "anthropic", "modelID": "claude-sonnet-4"}
    await server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 201, 202])
async def test_prompt_async_rejects_non_204(monkeypatch, status):
    app = web.Application()

    async def prompt(_):
        return web.json_response({"ok": True}, status=status)

    app.router.add_post("/session/ses-1/prompt_async", prompt)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server))
    client = OpenCodeClient(Settings.from_env())
    with pytest.raises(OpenCodeClientError):
        await client.prompt_async("ses-1", {"parts": [{"type": "text", "text": "hi"}], "model": "anthropic/claude-sonnet-4"})
    await server.close()


@pytest.mark.asyncio
async def test_put_auth_redacts_secret_on_exception(monkeypatch):
    monkeypatch.setenv("EFP_OPENCODE_URL", "http://127.0.0.1:9")
    result = await OpenCodeClient(Settings.from_env()).put_auth("anthropic", "SECRET-XYZ")
    assert result["success"] is False
    assert "SECRET-XYZ" not in result.get("error", "")


@pytest.mark.asyncio
async def test_patch_config_pending_restart(monkeypatch):
    app = web.Application()

    async def patch(_):
        return web.json_response({}, status=404)

    app.router.add_patch("/config", patch)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server))
    result = await OpenCodeClient(Settings.from_env()).patch_config({"a": 1})
    assert result["pending_restart"] is True
    await server.close()


@pytest.mark.asyncio
async def test_mcp_unsupported_returns_empty(monkeypatch):
    app = web.Application()

    async def mcp(_):
        return web.json_response({}, status=404)

    app.router.add_get("/mcp", mcp)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server))
    result = await OpenCodeClient(Settings.from_env()).mcp()
    assert result == {"success": False, "tools": [], "servers": {}}
    await server.close()


@pytest.mark.asyncio
async def test_mcp_servers_map_response(monkeypatch):
    app = web.Application()

    async def mcp(_):
        return web.json_response({"github": {"type": "local", "status": "connected"}}, status=200)

    app.router.add_get("/mcp", mcp)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server))
    result = await OpenCodeClient(Settings.from_env()).mcp()
    assert result["success"] is True
    assert result["tools"] == []
    assert result["servers"]["github"]["status"] == "connected"
    await server.close()


@pytest.mark.asyncio
async def test_mcp_legacy_tools_shape_still_supported(monkeypatch):
    app = web.Application()

    async def mcp(_):
        return web.json_response({"tools": ["a"], "servers": {"x": {"status": "connected"}}}, status=200)

    app.router.add_get("/mcp", mcp)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server))
    result = await OpenCodeClient(Settings.from_env()).mcp()
    assert result == {"success": True, "tools": ["a"], "servers": {"x": {"status": "connected"}}}
    await server.close()


@pytest.mark.asyncio
async def test_event_stream_parses_sse(monkeypatch):
    app = web.Application()

    async def events(request):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(b'event: server.connected\n')
        await resp.write(b'data: {"hello": true}\n\n')
        await resp.write_eof()
        return resp

    app.router.add_get("/event", events)
    server = TestServer(app)
    await server.start_server()

    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server))
    client = OpenCodeClient(Settings.from_env())
    events_iter = client.event_stream()
    try:
        first = await events_iter.__anext__()
        assert first["type"] == "server.connected"
        assert first["hello"] is True
    finally:
        await events_iter.aclose()
        await server.close()


@pytest.mark.asyncio
async def test_event_stream_partial_consume_can_be_closed_without_leaking_session(monkeypatch):
    app = web.Application()

    async def events(request):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(b'event: server.connected\n')
        await resp.write(b'data: {"hello": true}\n\n')
        await asyncio.sleep(10)
        return resp

    app.router.add_get("/event", events)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server))
    client = OpenCodeClient(Settings.from_env())
    events_iter = client.event_stream()
    try:
        first = await events_iter.__anext__()
        assert first["type"] == "server.connected"
        assert first["hello"] is True
    finally:
        await events_iter.aclose()
        await server.close()


@pytest.mark.asyncio
async def test_request_closes_owned_session_when_request_raises(monkeypatch):
    from efp_opencode_adapter import opencode_client as module

    sessions = []

    class FailingSession:
        def __init__(self):
            self.closed = False
            sessions.append(self)

        async def request(self, *args, **kwargs):
            raise RuntimeError("connection failed before response")

        async def close(self):
            self.closed = True

    monkeypatch.setattr(module.aiohttp, "ClientSession", FailingSession)

    client = OpenCodeClient(Settings.from_env())

    with pytest.raises(RuntimeError, match="connection failed before response"):
        await client._request("PUT", "http://127.0.0.1:9/auth/anthropic")

    assert len(sessions) == 1
    assert sessions[0].closed is True


@pytest.mark.asyncio
async def test_close_owned_response_releases_response_and_closes_session():
    from efp_opencode_adapter.opencode_client import _close_owned_response

    class FakeSession:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    class FakeResponse:
        def __init__(self):
            self.released = False
            self._efp_session = FakeSession()

        def release(self):
            self.released = True

    resp = FakeResponse()
    await _close_owned_response(resp)

    assert resp.released is True
    assert resp._efp_session.closed is True


@pytest.mark.asyncio
async def test_request_does_not_close_injected_session_when_request_raises():
    class InjectedFailingSession:
        def __init__(self):
            self.closed = False
            self.calls = 0

        async def request(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError("injected connection failed")

        async def close(self):
            self.closed = True

    session = InjectedFailingSession()
    client = OpenCodeClient(Settings.from_env(), session=session)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="injected connection failed"):
        await client._request("PUT", "http://127.0.0.1:9/auth/anthropic")

    assert session.calls == 1
    assert session.closed is False


@pytest.mark.asyncio
async def test_send_message_includes_message_id_no_reply_and_tools(monkeypatch):
    app = web.Application()
    captured = {}

    async def message(request):
        captured["body"] = await request.json()
        return web.json_response({"ok": True}, status=200)

    app.router.add_post("/session/ses-1/message", message)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server))
    client = OpenCodeClient(Settings.from_env())
    await client.send_message("ses-1", parts=[{"type": "text", "text": "hi"}], model="m", agent="a", message_id="m-1", no_reply=True, tools={"x": False})
    assert captured["body"]["messageID"] == "m-1"
    assert captured["body"]["noReply"] is True
    assert captured["body"]["tools"] == {"x": False}
    await server.close()


@pytest.mark.asyncio
async def test_fork_session_posts_message_id(monkeypatch):
    app = web.Application()
    captured = {}

    async def fork(request):
        captured["body"] = await request.json()
        return web.json_response({"id": "ses-2"})

    app.router.add_post("/session/ses-1/fork", fork)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server))
    out = await OpenCodeClient(Settings.from_env()).fork_session("ses-1", "m-prev")
    assert captured["body"] == {"messageID": "m-prev"}
    assert out == {"id": "ses-2"}
    await server.close()


@pytest.mark.asyncio
async def test_cancel_message_prefers_abort_session(monkeypatch):
    app = web.Application()
    calls: list[str] = []

    async def abort(_):
        calls.append("abort")
        return web.Response(status=204)

    async def cancel(_):
        calls.append("cancel")
        return web.json_response({}, status=200)

    app.router.add_post("/session/ses-1/abort", abort)
    app.router.add_post("/session/ses-1/cancel", cancel)
    app.router.add_post("/session/ses-1/message/m-1/cancel", cancel)
    app.router.add_post("/session/ses-1/message/m-1/abort", cancel)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server))
    out = await OpenCodeClient(Settings.from_env()).cancel_message("ses-1")
    assert out["success"] is True
    assert calls == ["abort"]
    await server.close()


@pytest.mark.asyncio
async def test_request_json_wraps_transport_error_as_opencode_client_error():
    class InjectedClientErrorSession:
        def request(self, *args, **kwargs):
            raise aiohttp.ClientError("connection refused")

    client = OpenCodeClient(Settings.from_env(), session=InjectedClientErrorSession())  # type: ignore[arg-type]
    with pytest.raises(OpenCodeClientError) as exc:
        await client.list_messages("ses-1")
    assert exc.value.status is None
    assert "transport error" in str(exc.value)


@pytest.mark.asyncio
async def test_request_json_with_status_wraps_timeout_as_opencode_client_error():
    class InjectedTimeoutSession:
        def request(self, *args, **kwargs):
            raise asyncio.TimeoutError()

    client = OpenCodeClient(Settings.from_env(), session=InjectedTimeoutSession())  # type: ignore[arg-type]
    with pytest.raises(OpenCodeClientError) as exc:
        await client.abort_session("ses-1")
    assert exc.value.status is None


@pytest.mark.asyncio
async def test_request_json_transport_error_includes_exception_type_for_timeout():
    class InjectedTimeoutSession:
        def request(self, *args, **kwargs):
            raise asyncio.TimeoutError()

    client = OpenCodeClient(Settings.from_env(), session=InjectedTimeoutSession())  # type: ignore[arg-type]
    with pytest.raises(OpenCodeClientError) as exc:
        await client.list_tool_ids()
    message = str(exc.value)
    assert "transport error" in message
    assert "TimeoutError" in message
    assert "TimeoutError()" in message


@pytest.mark.asyncio
async def test_request_json_with_status_transport_error_includes_exception_type_for_timeout():
    class InjectedTimeoutSession:
        def request(self, *args, **kwargs):
            raise asyncio.TimeoutError()

    client = OpenCodeClient(Settings.from_env(), session=InjectedTimeoutSession())  # type: ignore[arg-type]
    with pytest.raises(OpenCodeClientError) as exc:
        await client.abort_session("ses-1")
    message = str(exc.value)
    assert "transport error" in message
    assert "TimeoutError" in message
    assert "TimeoutError()" in message


@pytest.mark.asyncio
async def test_list_tool_ids_handles_list_response(monkeypatch):
    client = OpenCodeClient(Settings.from_env())

    async def fake_request_json(*args, **kwargs):
        return ["efp_smoke_tool"]

    monkeypatch.setattr(client, "_request_json", fake_request_json)
    assert await client.list_tool_ids() == ["efp_smoke_tool"]


@pytest.mark.asyncio
async def test_list_tool_ids_handles_ids_object_response(monkeypatch):
    client = OpenCodeClient(Settings.from_env())

    async def fake_request_json(*args, **kwargs):
        return {"ids": ["efp_smoke_tool"]}

    monkeypatch.setattr(client, "_request_json", fake_request_json)
    assert await client.list_tool_ids() == ["efp_smoke_tool"]


@pytest.mark.asyncio
async def test_list_tool_ids_handles_tools_object_response(monkeypatch):
    client = OpenCodeClient(Settings.from_env())

    async def fake_request_json(*args, **kwargs):
        return {"tools": ["efp_smoke_tool"]}

    monkeypatch.setattr(client, "_request_json", fake_request_json)
    assert await client.list_tool_ids() == ["efp_smoke_tool"]


@pytest.mark.asyncio
async def test_list_tool_ids_rejects_invalid_shape(monkeypatch):
    client = OpenCodeClient(Settings.from_env())

    async def fake_request_json(*args, **kwargs):
        return {"oops": True}

    monkeypatch.setattr(client, "_request_json", fake_request_json)
    with pytest.raises(OpenCodeClientError, match="unexpected tool ids response shape"):
        await client.list_tool_ids()

@pytest.mark.asyncio
async def test_put_auth_info_sends_oauth(monkeypatch):
    app = web.Application()
    async def put_auth(request: web.Request):
        assert request.headers.get("Authorization") is None
        body = await request.json()
        assert body == {"type": "oauth", "refresh": "gho_R", "access": "gho_A", "expires": 0}
        return web.json_response({}, status=200)
    app.router.add_put("/auth/github-copilot", put_auth)
    server = TestServer(app); await server.start_server()
    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server))
    result = await OpenCodeClient(Settings.from_env()).put_auth_info("github-copilot", {"type": "oauth", "refresh": "gho_R", "access": "gho_A", "expires": 0})
    assert result["success"] is True
    await server.close()


@pytest.mark.asyncio
async def test_send_message_model_ref_and_error_redaction(monkeypatch):
    captured = {}
    app = web.Application()
    async def post_msg(request: web.Request):
        captured["body"] = await request.json()
        return web.json_response({"ok": True}, status=200)
    app.router.add_post("/session/ses-1/message", post_msg)
    server = TestServer(app); await server.start_server()
    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server))
    client = OpenCodeClient(Settings.from_env())
    await client.send_message("ses-1", parts=[{"type":"text","text":"hi"}], model="github_copilot/gpt-5.4-mini", agent="efp-main")
    assert captured["body"]["model"] == {"providerID": "github-copilot", "modelID": "gpt-5.4-mini"}
    await server.close()

    app2 = web.Application()
    async def bad(_request: web.Request):
        return web.json_response({"error": "bad", "access": "gho_SECRET", "refresh": "gho_SECRET", "detail": "token=ghu_SECRET"}, status=400)
    app2.router.add_post("/session/ses-2/message", bad)
    server2 = TestServer(app2); await server2.start_server()
    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server2))
    with pytest.raises(OpenCodeClientError) as exc:
        await OpenCodeClient(Settings.from_env()).send_message("ses-2", parts=[{"type":"text","text":"hi"}], model="gpt-5.4-mini", agent="efp-main")
    msg = str(exc.value)
    assert "status 400" in msg
    assert "gho_SECRET" not in msg and "ghu_SECRET" not in msg
    await server2.close()


@pytest.mark.asyncio
async def test_send_message_rejects_201(monkeypatch):
    app = web.Application()

    async def post_msg(_request: web.Request):
        return web.json_response({"ok": True}, status=201)

    app.router.add_post("/session/ses-1/message", post_msg)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server))
    with pytest.raises(OpenCodeClientError):
        await OpenCodeClient(Settings.from_env()).send_message(
            "ses-1", parts=[{"type": "text", "text": "hi"}], model="anthropic/claude-sonnet-4", agent="efp-main"
        )
    await server.close()


@pytest.mark.asyncio
async def test_transport_error_message_redacts_secret(monkeypatch):
    class FailingRequestCtx:
        async def __aenter__(self):
            raise aiohttp.ClientError("failed gho_SECRET sk-1234567890abcdef")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FailingSession:
        def request(self, *args, **kwargs):
            return FailingRequestCtx()

    monkeypatch.setenv("EFP_OPENCODE_URL", "http://127.0.0.1:9")
    client = OpenCodeClient(Settings.from_env(), session=FailingSession())
    with pytest.raises(OpenCodeClientError) as exc:
        await client._request_json("GET", "/global/health")
    text = str(exc.value)
    assert "gho_SECRET" not in text
    assert "sk-1234567890abcdef" not in text
    payload_dump = json.dumps(exc.value.payload)
    assert "gho_SECRET" not in payload_dump
    assert "sk-1234567890abcdef" not in payload_dump


def test_safe_error_preview_redacts_json_like_access_refresh_text():
    text = _safe_error_preview('{"access":"abc123","refresh":"def456","token":"tok789"}')
    assert "abc123" not in text
    assert "def456" not in text
    assert "tok789" not in text
    assert "***REDACTED***" in text


def test_safe_error_preview_does_not_redact_short_sk_substring():
    assert _safe_error_preview("agent-task-1") == "agent-task-1"


def test_safe_error_preview_redacts_real_sk_token():
    text = _safe_error_preview("bad sk-1234567890abcdef")
    assert "***REDACTED***" in text
    assert "sk-1234567890abcdef" not in text


@pytest.mark.asyncio
async def test_text_plain_error_body_redacts_json_like_sensitive_keys(monkeypatch):
    app = web.Application()

    async def bad(_request: web.Request):
        return web.Response(status=400, text='{"error":"bad","access":"abc123","refresh":"def456"}', content_type="text/plain")

    app.router.add_post("/session/ses-plain/message", bad)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server))
    with pytest.raises(OpenCodeClientError) as exc:
        await OpenCodeClient(Settings.from_env()).send_message("ses-plain", parts=[{"type": "text", "text": "hi"}], model="github-copilot/gpt-5.4-mini", agent="efp-main")
    message = str(exc.value)
    assert "status 400" in message
    assert "abc123" not in message
    assert "def456" not in message
    payload_dump = json.dumps(exc.value.payload)
    assert "abc123" not in payload_dump
    assert "def456" not in payload_dump
    await server.close()

@pytest.mark.asyncio
async def test_send_message_does_not_add_copilot_integration_header(monkeypatch):
    app = web.Application()

    async def post_message(request: web.Request):
        assert request.headers.get("copilot-integration-id") is None
        body = await request.json()
        assert "headers" not in body
        return web.json_response({"ok": True}, status=200)

    app.router.add_post("/session/ses-1/message", post_message)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server))

    await OpenCodeClient(Settings.from_env()).send_message(
        "ses-1",
        parts=[{"type": "text", "text": "hi"}],
        model="github-copilot/gpt-5",
        agent="efp-main",
    )
    await server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["github-copilot/gpt-x", "github_copilot/gpt-x", "copilot/gpt-x", "github/gpt-x"])
async def test_send_message_does_not_add_copilot_header_for_aliases(monkeypatch, model):
    app = web.Application()

    async def post_message(request: web.Request):
        assert request.headers.get("copilot-integration-id") is None
        body = await request.json()
        assert "headers" not in body
        return web.json_response({"ok": True}, status=200)

    app.router.add_post("/session/ses-1/message", post_message)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server))

    await OpenCodeClient(Settings.from_env()).send_message("ses-1", parts=[{"type": "text", "text": "hi"}], model=model, agent="efp-main")
    await server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["openai/gpt-5", "anthropic/claude-sonnet-4-5"])
async def test_send_message_does_not_add_copilot_header_for_non_copilot(monkeypatch, model):
    app = web.Application()

    async def post_message(request: web.Request):
        assert request.headers.get("copilot-integration-id") is None
        return web.json_response({"ok": True}, status=200)

    app.router.add_post("/session/ses-1/message", post_message)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server))

    await OpenCodeClient(Settings.from_env()).send_message("ses-1", parts=[{"type": "text", "text": "hi"}], model=model, agent="efp-main")
    await server.close()
