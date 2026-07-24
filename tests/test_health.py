import shutil

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter import state as state_mod
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.state import (
    STATE_HEALTH_CACHE_TTL_SECONDS,
    build_state_health_snapshot,
    ensure_state_dirs,
    reset_state_health_cache,
)


class FakeHealthyClient:
    async def health(self):
        return {"healthy": True, "version": "9.9.9"}


class FakeUnhealthyClient:
    async def health(self):
        return {"healthy": False, "error": "down"}


@pytest.mark.asyncio
async def test_health_ok(monkeypatch):
    monkeypatch.setenv("OPENCODE_VERSION", "1.14.39")
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
        assert payload["opencode_version"] == "9.9.9"
        assert payload["opencode"]["version"] == "9.9.9"
        assert payload["opencode"]["healthy"] is True
        assert payload["state"]["healthy"] is True

    await client.close()


@pytest.mark.asyncio
async def test_health_reports_observed_opencode_version_without_enforcing_config(monkeypatch):
    monkeypatch.setenv("OPENCODE_VERSION", "1.14.39")
    app = create_app(Settings.from_env(), opencode_client=FakeHealthyClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    resp = await client.get("/health")
    assert resp.status == 200
    payload = await resp.json()
    assert payload["opencode_version"] == "9.9.9"
    assert payload["opencode"]["version"] == "9.9.9"
    await client.close()


@pytest.mark.asyncio
async def test_health_degraded(monkeypatch):
    monkeypatch.setenv("OPENCODE_VERSION", "1.14.39")
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
        assert payload["opencode_version"] == Settings.from_env().opencode_version
        assert payload["opencode"]["healthy"] is False
        assert payload["opencode"]["error"]


@pytest.mark.asyncio
async def test_health_degraded_when_state_unwritable(monkeypatch):
    monkeypatch.setenv("OPENCODE_VERSION", "1.14.39")
    app = create_app(Settings.from_env(), opencode_client=FakeHealthyClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    from efp_opencode_adapter import state as state_mod
    orig = state_mod.probe_writable
    state_mod.probe_writable = lambda p: {"path": str(p), "exists": True, "writable": False, "error": "forced"}
    resp = await client.get("/health")
    payload = await resp.json()
    assert resp.status == 503
    assert payload["state"]["healthy"] is False
    state_mod.probe_writable = orig
    await client.close()

    await client.close()


class SecretErrorClient:
    async def health(self):
        return {"healthy": False, "error": "failed with api_key SECRET-KEY-SHOULD-NOT-LEAK token"}


@pytest.mark.asyncio
async def test_health_degraded_sanitizes_secret_error(monkeypatch):
    monkeypatch.setenv("OPENCODE_VERSION", "1.14.39")
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


class RaisingHealthClient:
    async def health(self):
        raise RuntimeError("health boom api_key SECRET-KEY-SHOULD-NOT-LEAK token")


@pytest.mark.asyncio
async def test_health_client_exception_is_degraded_and_does_not_leak_secret(monkeypatch, capsys):
    monkeypatch.setenv("OPENCODE_VERSION", "1.14.39")
    app = create_app(Settings.from_env(), opencode_client=RaisingHealthClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    resp = await client.get("/health")
    text = await resp.text()

    assert resp.status == 503
    assert "api_key" not in text.lower()
    assert "SECRET-KEY-SHOULD-NOT-LEAK" not in text
    assert "token" not in text.lower()

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "SECRET-KEY-SHOULD-NOT-LEAK" not in combined
    assert "api_key" not in combined.lower()
    assert "token" not in combined.lower()

    await client.close()


def test_state_health_probe_is_cached_for_a_short_window():
    """k8s and the actuator scanners poll health several times a second; each
    snapshot writes probe files into six PVC directories, so a burst must cost
    one pass -- while a PVC that turns unusable is still reported within the TTL."""
    settings = Settings.from_env()
    paths = ensure_state_dirs(settings)
    reset_state_health_cache()

    assert build_state_health_snapshot(settings, paths, now=100.0)["healthy"] is True

    # The PVC turns unusable: probing chatlogs_dir now fails, because it is a file.
    shutil.rmtree(paths.chatlogs_dir)
    paths.chatlogs_dir.write_text("not a directory", encoding="utf-8")

    inside_ttl = build_state_health_snapshot(settings, paths, now=100.0 + STATE_HEALTH_CACHE_TTL_SECONDS - 0.01)
    assert inside_ttl["healthy"] is True

    after_ttl = build_state_health_snapshot(settings, paths, now=100.0 + STATE_HEALTH_CACHE_TTL_SECONDS + 0.01)
    assert after_ttl["healthy"] is False
    assert after_ttl["paths"]["chatlogs_dir"]["writable"] is False


@pytest.mark.asyncio
async def test_repeated_health_probes_do_not_touch_the_filesystem_again(monkeypatch):
    reset_state_health_cache()
    app = create_app(Settings.from_env(), opencode_client=FakeHealthyClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        assert (await client.get("/health")).status == 200

        probed = []
        original = state_mod.probe_writable
        monkeypatch.setattr(state_mod, "probe_writable", lambda p: (probed.append(p), original(p))[1])
        for _ in range(10):
            assert (await client.get("/actuator/health")).status == 200

        assert probed == []
    finally:
        await client.close()
