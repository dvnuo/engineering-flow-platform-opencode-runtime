import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from efp_opencode_adapter.opencode_client import OpenCodeClient
from efp_opencode_adapter.settings import Settings


@pytest.mark.asyncio
async def test_health_and_wait_ready(monkeypatch):
    app = web.Application()

    async def h(_):
        return web.json_response({"healthy": True, "version": "1.14.29"})

    app.router.add_get("/global/health", h)
    server = TestServer(app)
    await server.start_server()

    monkeypatch.setenv("EFP_OPENCODE_URL", str(server.make_url(""))[:-1])
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

    monkeypatch.setenv("EFP_OPENCODE_URL", str(server.make_url(""))[:-1])
    monkeypatch.setenv("OPENCODE_VERSION", "1.14.29")
    settings = Settings.from_env()
    client = OpenCodeClient(settings)
    with pytest.raises(RuntimeError, match="version mismatch"):
        await client.wait_until_ready(timeout_seconds=1)
    await server.close()
