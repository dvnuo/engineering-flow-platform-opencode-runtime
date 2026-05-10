import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from efp_opencode_adapter.portal_metadata_client import PortalMetadataClient
from efp_opencode_adapter.settings import Settings


def _base(server: TestServer) -> str:
    return str(server.make_url("")).rstrip("/")


def test_settings_accepts_float_portal_metadata_timeout(monkeypatch):
    monkeypatch.setenv("PORTAL_METADATA_TIMEOUT_SECONDS", "0.01")
    assert Settings.from_env().portal_metadata_timeout_seconds == 0.01


@pytest.mark.asyncio
async def test_delete_session_metadata_skipped_when_not_configured(monkeypatch):
    monkeypatch.delenv("PORTAL_INTERNAL_BASE_URL", raising=False)
    monkeypatch.delenv("PORTAL_AGENT_ID", raising=False)
    out = await PortalMetadataClient(Settings.from_env()).delete_session_metadata("s1")
    assert out == {"success": False, "skipped": True, "reason": "portal_metadata_not_configured"}


@pytest.mark.asyncio
async def test_delete_session_metadata_2xx_json_and_headers_and_encoding(monkeypatch):
    got = {}
    async def ok(request):
        got["raw_path"] = request.raw_path
        got["path"] = request.path
        got["auth"] = request.headers.get("Authorization")
        got["token"] = request.headers.get("X-Portal-Internal-Token")
        return web.json_response({"ok": True}, status=200)
    app = web.Application()
    app.router.add_delete("/{tail:.*}", ok)
    server = TestServer(app); await server.start_server()
    monkeypatch.setenv("PORTAL_INTERNAL_BASE_URL", _base(server))
    monkeypatch.setenv("PORTAL_AGENT_ID", "agent 1")
    monkeypatch.setenv("PORTAL_INTERNAL_TOKEN", "tok")
    out = await PortalMetadataClient(Settings.from_env()).delete_session_metadata("s/1 x%2")
    assert out["success"] is True and out["status"] == 200 and out["payload"] == {"ok": True}
    assert got["auth"] == "Bearer tok" and got["token"] == "tok"
    assert got["raw_path"] == "/api/internal/agents/agent%201/sessions/s%2F1%20x%252/metadata"
    await server.close()


@pytest.mark.asyncio
async def test_delete_session_metadata_2xx_non_json_payload_none(monkeypatch):
    async def ok(_):
        return web.Response(text="ok", status=204)
    app = web.Application(); app.router.add_delete("/api/internal/agents/agent/sessions/s1/metadata", ok)
    server = TestServer(app); await server.start_server()
    monkeypatch.setenv("PORTAL_INTERNAL_BASE_URL", _base(server)); monkeypatch.setenv("PORTAL_AGENT_ID", "agent")
    out = await PortalMetadataClient(Settings.from_env()).delete_session_metadata("s1")
    assert out["success"] is True and out["payload"] is None
    await server.close()


@pytest.mark.asyncio
async def test_delete_session_metadata_404_405_and_500(monkeypatch):
    async def missing(_): return web.Response(status=404)
    async def method(_): return web.Response(status=405)
    async def err(_): return web.Response(status=500, text="boom")
    app = web.Application()
    app.router.add_delete("/missing/api/internal/agents/agent/sessions/s1/metadata", missing)
    app.router.add_delete("/method/api/internal/agents/agent/sessions/s1/metadata", method)
    app.router.add_delete("/err/api/internal/agents/agent/sessions/s1/metadata", err)
    server = TestServer(app); await server.start_server()
    monkeypatch.setenv("PORTAL_AGENT_ID", "agent")
    monkeypatch.setenv("PORTAL_INTERNAL_BASE_URL", _base(server)+"/missing")
    out = await PortalMetadataClient(Settings.from_env()).delete_session_metadata("s1")
    assert out["skipped"] is True and out["reason"] == "portal_metadata_delete_endpoint_unavailable"
    monkeypatch.setenv("PORTAL_INTERNAL_BASE_URL", _base(server)+"/method")
    out = await PortalMetadataClient(Settings.from_env()).delete_session_metadata("s1")
    assert out["skipped"] is True and out["status"] == 405
    monkeypatch.setenv("PORTAL_INTERNAL_BASE_URL", _base(server)+"/err")
    out = await PortalMetadataClient(Settings.from_env()).delete_session_metadata("s1")
    assert out["success"] is False and out["status"] == 500
    await server.close()


@pytest.mark.asyncio
async def test_delete_session_metadata_timeout(monkeypatch):
    async def slow(_):
        await asyncio.sleep(0.05)
        return web.json_response({"ok": True})
    app = web.Application(); app.router.add_delete("/api/internal/agents/agent/sessions/s1/metadata", slow)
    server = TestServer(app); await server.start_server()
    monkeypatch.setenv("PORTAL_AGENT_ID", "agent")
    monkeypatch.setenv("PORTAL_INTERNAL_BASE_URL", _base(server))
    monkeypatch.setenv("PORTAL_METADATA_TIMEOUT_SECONDS", "0.01")
    out = await PortalMetadataClient(Settings.from_env()).delete_session_metadata("s1")
    assert out["success"] is False and "error" in out
    await server.close()


@pytest.mark.asyncio
async def test_publish_session_metadata_put_uses_quoted_raw_path_and_headers(monkeypatch):
    got = {}

    async def ok(request):
        got["raw_path"] = request.raw_path
        got["path"] = request.path
        got["auth"] = request.headers.get("Authorization")
        got["token"] = request.headers.get("X-Portal-Internal-Token")
        return web.json_response({"ok": True}, status=200)

    app = web.Application()
    app.router.add_put("/{tail:.*}", ok)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("PORTAL_INTERNAL_BASE_URL", _base(server))
    monkeypatch.setenv("PORTAL_AGENT_ID", "agent 1")
    monkeypatch.setenv("PORTAL_INTERNAL_TOKEN", "tok")

    out = await PortalMetadataClient(Settings.from_env()).publish_session_metadata(
        session_id="s/1 x%2",
        latest_event_type="chat.completed",
        latest_event_state="done",
    )

    assert out["success"] is True and out["status"] == 200
    assert got["auth"] == "Bearer tok" and got["token"] == "tok"
    assert got["raw_path"] == "/api/internal/agents/agent%201/sessions/s%2F1%20x%252/metadata"
    await server.close()
