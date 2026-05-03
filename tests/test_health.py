import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings


class FakeHealthyClient:
    async def health(self):
        return {"healthy": True, "version": "1.14.29"}


class FakeUnhealthyClient:
    async def health(self):
        return {"healthy": False, "error": "down"}


@pytest.mark.asyncio
async def test_health_ok(monkeypatch):
    monkeypatch.setenv("OPENCODE_VERSION", "1.14.29")
    app = create_app(Settings.from_env(), opencode_client=FakeHealthyClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    for endpoint in ["/health", "/actuator/health"]:
        resp = await client.get(endpoint)
        assert resp.status == 200
        payload = await resp.json()
        assert payload["status"] == "ok"
        assert payload["service"] == "efp-opencode-runtime"
        assert payload["engine"] == "opencode"
        assert payload["opencode_version"] == "1.14.29"
        assert payload["opencode"]["healthy"] is True

    await client.close()


@pytest.mark.asyncio
async def test_health_degraded(monkeypatch):
    monkeypatch.setenv("OPENCODE_VERSION", "1.14.29")
    app = create_app(Settings.from_env(), opencode_client=FakeUnhealthyClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    for endpoint in ["/health", "/actuator/health"]:
        resp = await client.get(endpoint)
        assert resp.status == 503
        payload = await resp.json()
        assert payload["status"] == "degraded"
        assert payload["service"] == "efp-opencode-runtime"
        assert payload["engine"] == "opencode"
        assert payload["opencode_version"] == "1.14.29"
        assert payload["opencode"]["healthy"] is False
        assert payload["opencode"]["error"]

    await client.close()


class SecretErrorClient:
    async def health(self):
        return {"healthy": False, "error": "failed with api_key SECRET-KEY-SHOULD-NOT-LEAK token"}


@pytest.mark.asyncio
async def test_health_degraded_sanitizes_secret_error(monkeypatch):
    monkeypatch.setenv("OPENCODE_VERSION", "1.14.29")
    app = create_app(Settings.from_env(), opencode_client=SecretErrorClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    resp = await client.get("/health")
    assert resp.status == 503
    encoded = await resp.text()
    assert "api_key" not in encoded.lower()
    assert "SECRET-KEY-SHOULD-NOT-LEAK" not in encoded
    assert "token" not in encoded.lower()
    await client.close()
