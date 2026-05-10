import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from efp_opencode_adapter.portal_metadata_client import PortalMetadataClient
from efp_opencode_adapter.settings import Settings


def _base(server: TestServer) -> str:
    return str(server.make_url(""))[:-1]


@pytest.mark.asyncio
async def test_delete_session_metadata_skipped_when_not_configured(monkeypatch):
    monkeypatch.delenv("PORTAL_INTERNAL_BASE_URL", raising=False)
    monkeypatch.delenv("PORTAL_AGENT_ID", raising=False)
    out = await PortalMetadataClient(Settings.from_env()).delete_session_metadata("s1")
    assert out["skipped"] is True


@pytest.mark.asyncio
async def test_delete_session_metadata_variants(monkeypatch):
    async def ok(request):
        assert request.headers.get("X-Portal-Internal-Token") == "tok"
        return web.json_response({"ok": True})
    async def missing(_): return web.Response(status=404)
    async def method(_): return web.Response(status=405)
    async def err(_): return web.Response(status=500, text="boom")
    app = web.Application()
    app.router.add_delete("/ok/api/internal/agents/agent/sessions/s1/metadata", ok)
    app.router.add_delete("/missing/api/internal/agents/agent/sessions/s1/metadata", missing)
    app.router.add_delete("/method/api/internal/agents/agent/sessions/s1/metadata", method)
    app.router.add_delete("/err/api/internal/agents/agent/sessions/s1/metadata", err)
    server = TestServer(app); await server.start_server()
    monkeypatch.setenv("PORTAL_AGENT_ID", "agent"); monkeypatch.setenv("PORTAL_INTERNAL_TOKEN", "tok")
    for path, key in [("/ok", "success"), ("/missing", "skipped"), ("/method", "skipped"), ("/err", "success")]:
        monkeypatch.setenv("PORTAL_INTERNAL_BASE_URL", _base(server)+path)
        out = await PortalMetadataClient(Settings.from_env()).delete_session_metadata("s1")
        if path == "/ok": assert out["success"] is True
        elif path in {"/missing", "/method"}: assert out["skipped"] is True
        else: assert out["success"] is False and out["status"] == 500
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
    assert out["success"] is False
    await server.close()
