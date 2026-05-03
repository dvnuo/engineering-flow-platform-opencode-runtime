import base64

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from efp_opencode_adapter.opencode_client import OpenCodeClient
from efp_opencode_adapter.settings import Settings


def server_base_url(server: TestServer) -> str:
    return str(server.make_url("")).rstrip("/")


@pytest.mark.asyncio
async def test_health_and_wait_ready(monkeypatch):
    app = web.Application()

    async def h(_):
        return web.json_response({"healthy": True, "version": "1.14.29"})

    app.router.add_get("/global/health", h)
    server = TestServer(app)
    await server.start_server()

    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server))
    settings = Settings.from_env()
    client = OpenCodeClient(settings)
    health = await client.health()
    assert health["healthy"] is True
    assert health["version"] == "1.14.29"
    await client.wait_until_ready(timeout_seconds=1)
    await server.close()


@pytest.mark.asyncio
async def test_unreachable_degraded(monkeypatch):
    monkeypatch.setenv("EFP_OPENCODE_URL", "http://127.0.0.1:9")
    settings = Settings.from_env()
    client = OpenCodeClient(settings)
    health = await client.health()
    assert health["healthy"] is False
    with pytest.raises(TimeoutError):
        await client.wait_until_ready(timeout_seconds=1)


@pytest.mark.asyncio
async def test_version_mismatch(monkeypatch):
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
    with pytest.raises(RuntimeError, match="version mismatch"):
        await client.wait_until_ready(timeout_seconds=1)
    await server.close()


@pytest.mark.asyncio
async def test_health_uses_basic_auth_when_password_set(monkeypatch):
    app = web.Application()
    expected = "Basic " + base64.b64encode(b"opencode:test-password").decode()

    async def h(request: web.Request):
        if request.headers.get("Authorization") != expected:
            return web.json_response({"healthy": False}, status=401)
        return web.json_response({"healthy": True, "version": "1.14.29"})

    app.router.add_get("/global/health", h)
    server = TestServer(app)
    await server.start_server()

    monkeypatch.setenv("OPENCODE_SERVER_USERNAME", "opencode")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "test-password")
    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server))

    client = OpenCodeClient(Settings.from_env())
    health = await client.health()
    assert health["healthy"] is True
    assert health["version"] == "1.14.29"
    await server.close()


@pytest.mark.asyncio
async def test_put_auth_uses_basic_auth(monkeypatch):
    app = web.Application()
    expected = "Basic " + base64.b64encode(b"opencode:test-password").decode()

    async def put_auth(request: web.Request):
        if request.headers.get("Authorization") != expected:
            return web.json_response({}, status=401)
        return web.json_response({}, status=200)

    app.router.add_put("/auth/anthropic", put_auth)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("OPENCODE_SERVER_USERNAME", "opencode")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "test-password")
    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server))
    result = await OpenCodeClient(Settings.from_env()).put_auth("anthropic", "secret-value")
    assert result["success"] is True
    await server.close()


@pytest.mark.asyncio
async def test_prompt_async_accepts_204(monkeypatch):
    app = web.Application()

    async def prompt(_):
        return web.Response(status=204)

    app.router.add_post("/session/ses-1/prompt_async", prompt)
    server = TestServer(app)
    await server.start_server()

    monkeypatch.setenv("EFP_OPENCODE_URL", server_base_url(server))
    client = OpenCodeClient(Settings.from_env())
    result = await client.prompt_async("ses-1", {"parts": [{"type": "text", "text": "hi"}]})
    assert result is None
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
    assert result == {"success": False, "tools": []}
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
    first = await events_iter.__anext__()
    assert first["type"] == "server.connected"
    assert first["hello"] is True
    await server.close()
