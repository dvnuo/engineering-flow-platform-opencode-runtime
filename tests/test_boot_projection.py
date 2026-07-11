import json
import os

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.copilot_plugin_auth import copilot_plugin_auth_path
from efp_opencode_adapter.portal_runtime_context_bootstrap import apply_boot_projection, run_boot_projection_from_env
from efp_opencode_adapter.profile_store import ProfileOverlayStore
from efp_opencode_adapter.runtime_env import strip_managed_external_env
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import (
    ProfileEnvError,
    Settings,
    load_profile_env_payload,
    profile_env_profile_id,
    profile_env_revision,
)


def _payload(config: dict, profile_id="rp-1", revision=3) -> dict:
    return {
        "runtime_profile_id": profile_id,
        "name": "profile",
        "revision": revision,
        "runtime_type": "opencode",
        "config": config,
    }


class FakeHealthyClient:
    async def health(self):
        return {"healthy": True, "version": "1.14.39"}


class FakeUnhealthyClient:
    async def health(self):
        return {"healthy": False, "error": "down"}


class FakeProcessManager:
    def __init__(self):
        self.start_calls = []
        self.event_bus = None

    async def start(self, env=None, *, reason="startup"):
        self.start_calls.append({
            "env": dict(env or {}),
            "reason": reason,
            "blob_in_environ": "EFP_PROFILE_CONFIG" in os.environ,
        })
        return {"running": True}

    async def stop(self):
        return {}

    def status_snapshot(self):
        return {"managed": True}


def test_missing_profile_env_is_fatal(monkeypatch):
    monkeypatch.delenv("EFP_PROFILE_CONFIG", raising=False)
    with pytest.raises(ProfileEnvError, match="EFP_PROFILE_CONFIG is not set"):
        load_profile_env_payload()
    with pytest.raises(ProfileEnvError):
        run_boot_projection_from_env(Settings.from_env())


def test_invalid_profile_env_json_is_fatal(monkeypatch):
    monkeypatch.setenv("EFP_PROFILE_CONFIG", "{not json")
    with pytest.raises(ProfileEnvError, match="not valid JSON"):
        load_profile_env_payload()
    monkeypatch.setenv("EFP_PROFILE_CONFIG", '["not", "an", "object"]')
    with pytest.raises(ProfileEnvError, match="must be a JSON object"):
        load_profile_env_payload()


def test_profile_env_identity_helpers(monkeypatch):
    monkeypatch.setenv("EFP_PROFILE_REVISION", "7")
    monkeypatch.setenv("EFP_PROFILE_ID", "rp-9")
    assert profile_env_revision() == 7
    assert profile_env_profile_id() == "rp-9"
    monkeypatch.setenv("EFP_PROFILE_REVISION", "")
    monkeypatch.setenv("EFP_PROFILE_ID", "none")
    assert profile_env_revision() is None
    assert profile_env_profile_id() is None


def test_empty_config_payload_is_valid_boot(monkeypatch):
    monkeypatch.setenv(
        "EFP_PROFILE_CONFIG",
        json.dumps({"runtime_profile_id": None, "name": "", "revision": None, "runtime_type": "opencode", "config": {}}),
    )
    settings = Settings.from_env()
    result = run_boot_projection_from_env(settings)
    assert result.runtime_profile_id is None
    assert result.revision is None
    assert settings.opencode_config_path.exists()
    assert (settings.adapter_state_dir / "opencode.env").exists()
    overlay = ProfileOverlayStore(settings).load()
    assert overlay is not None and overlay.revision is None


def test_boot_projection_writes_files_env_and_copilot_credential(capsys):
    settings = Settings.from_env()
    config = {
        "llm": {
            "provider": "github_copilot",
            "model": "gpt-x",
            "api_key": "gho_TEST",
            "base_url": "http://litellm.local/v1",
        },
        "github": {"enabled": True, "username": "efp-bot", "token": "github_pat_test"},
    }
    result = apply_boot_projection(settings, _payload(config))

    cfg_path = settings.opencode_config_path
    assert cfg_path.exists()
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg["agent"]["efp-main"]["model"] == "github-copilot/gpt-x"
    assert cfg["provider"]["github-copilot"]["options"]["baseURL"] == "http://127.0.0.1:8000/api/internal/copilot"
    assert "gho_TEST" not in cfg_path.read_text(encoding="utf-8")

    state_payload = json.loads(copilot_plugin_auth_path(settings).read_text(encoding="utf-8"))
    assert state_payload["credential"] == "gho_TEST"
    assert not (settings.opencode_data_dir / "auth.json").exists()

    assert result.env["GH_TOKEN"] == "github_pat_test"
    assert result.env["GIT_USERNAME"] == "efp-bot"
    assert result.copilot_credential_present is True
    assert result.auth_written is False

    overlay = ProfileOverlayStore(settings).load()
    assert overlay is not None
    assert overlay.runtime_profile_id == "rp-1"
    assert overlay.revision == 3
    assert overlay.env_hash == result.env_hash
    assert "gho_TEST" not in json.dumps(overlay.config)

    emitted = capsys.readouterr()
    assert "gho_TEST" not in emitted.out
    assert "gho_TEST" not in emitted.err


def test_boot_projection_overwrites_stale_overlay(monkeypatch):
    settings = Settings.from_env()
    apply_boot_projection(settings, _payload({}, profile_id="rp-old", revision=1))
    apply_boot_projection(settings, _payload({}, profile_id="rp-new", revision=9))
    overlay = ProfileOverlayStore(settings).load()
    assert overlay is not None
    assert overlay.runtime_profile_id == "rp-new"
    assert overlay.revision == 9


def test_strip_managed_external_env_scrubs_profile_blob(monkeypatch):
    monkeypatch.setenv("EFP_PROFILE_CONFIG", "{}")
    monkeypatch.setenv("PATH", "/usr/bin")
    stripped = strip_managed_external_env(os.environ)
    assert "EFP_PROFILE_CONFIG" not in stripped
    assert stripped["PATH"] == "/usr/bin"


@pytest.mark.asyncio
async def test_managed_startup_projects_scrubs_and_gates_ready(monkeypatch):
    monkeypatch.setenv("EFP_PROFILE_CONFIG", json.dumps(_payload({"github": {"enabled": True, "token": "github_pat_boot"}})))
    monkeypatch.setenv("EFP_PROFILE_REVISION", "5")
    monkeypatch.setenv("EFP_PROFILE_ID", "rp-1")
    settings = Settings.from_env()
    manager = FakeProcessManager()
    app = create_app(settings, opencode_client=FakeHealthyClient(), opencode_process_manager=manager)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        # Projection ran before the managed start and the blob was scrubbed first.
        assert len(manager.start_calls) == 1
        start_call = manager.start_calls[0]
        assert start_call["reason"] == "startup"
        assert start_call["blob_in_environ"] is False
        assert start_call["env"]["GH_TOKEN"] == "github_pat_boot"
        assert "EFP_PROFILE_CONFIG" not in start_call["env"]
        assert "EFP_PROFILE_CONFIG" not in os.environ

        resp = await client.get("/ready")
        payload = await resp.json()
        assert resp.status == 200
        assert payload == {"ready": True, "runtime_profile_id": "rp-1", "revision": 5}

        health = await client.get("/health")
        health_payload = await health.json()
        assert health.status == 200
        assert health_payload["profile"]["boot_projection_complete"] is True
        assert health_payload["profile"]["runtime_profile_id"] == "rp-1"
        assert health_payload["profile"]["revision"] == 5

        status = await (await client.get("/api/internal/runtime-profile/status")).json()
        assert status["runtime_profile_id"] == "rp-1"
        assert status["revision"] == 5
        assert status["boot_projection"]["complete"] is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_managed_startup_with_missing_env_stays_unready(monkeypatch):
    monkeypatch.delenv("EFP_PROFILE_CONFIG", raising=False)
    settings = Settings.from_env()
    manager = FakeProcessManager()
    app = create_app(settings, opencode_client=FakeHealthyClient(), opencode_process_manager=manager)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        # Projection failure keeps the process alive but never starts opencode.
        assert manager.start_calls == []

        resp = await client.get("/ready")
        payload = await resp.json()
        assert resp.status == 503
        assert payload["ready"] is False
        assert "EFP_PROFILE_CONFIG" in payload["error"]

        health = await client.get("/health")
        health_payload = await health.json()
        assert health.status == 503
        assert health_payload["profile"]["boot_projection_complete"] is False

        status = await (await client.get("/api/internal/runtime-profile/status")).json()
        assert status["boot_projection"]["complete"] is False
        assert "EFP_PROFILE_CONFIG" in (status["boot_projection"]["error"] or "")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ready_requires_projection_and_healthy_opencode(monkeypatch):
    # Unmanaged app without a boot projection snapshot: never ready.
    app = create_app(Settings.from_env(), opencode_client=FakeHealthyClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/ready")
        payload = await resp.json()
        assert resp.status == 503
        assert payload["ready"] is False
        # /health stays governed by opencode/state health in unmanaged mode.
        assert (await client.get("/health")).status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ready_503_when_opencode_unhealthy_after_projection(monkeypatch):
    monkeypatch.setenv("EFP_PROFILE_CONFIG", json.dumps(_payload({})))
    settings = Settings.from_env()
    manager = FakeProcessManager()
    app = create_app(settings, opencode_client=FakeUnhealthyClient(), opencode_process_manager=manager)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/ready")
        payload = await resp.json()
        assert resp.status == 503
        assert payload["ready"] is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_status_handler_reports_env_revision_without_boot(monkeypatch):
    monkeypatch.setenv("EFP_PROFILE_REVISION", "11")
    monkeypatch.setenv("EFP_PROFILE_ID", "rp-env")
    app = create_app(Settings.from_env(), opencode_client=FakeHealthyClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        status = await (await client.get("/api/internal/runtime-profile/status")).json()
        assert status["engine"] == "opencode"
        assert status["runtime_profile_id"] == "rp-env"
        assert status["revision"] == 11
    finally:
        await client.close()
