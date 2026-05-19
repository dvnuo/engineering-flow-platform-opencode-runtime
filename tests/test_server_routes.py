import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.app_keys import EVENT_BUS_KEY
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


def test_chat_stream_and_events_routes_still_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())

    routes = {(route.method, route.resource.canonical) for route in app.router.routes()}

    assert ("POST", "/api/chat") in routes
    assert ("POST", "/api/chat/stream") in routes
    assert ("GET", "/api/events") in routes


def test_skills_resync_route_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())

    routes = {(route.method, route.resource.canonical) for route in app.router.routes()}

    assert ("POST", "/api/internal/skills/resync") in routes


def test_event_bus_uses_replay_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_EVENT_REPLAY_LIMIT", "17")
    monkeypatch.setenv("EFP_EVENT_REPLAY_TTL_SECONDS", "23")

    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    bus = app[EVENT_BUS_KEY]

    assert bus.replay_limit == 17
    assert bus.replay_ttl_seconds == 23


@pytest.mark.asyncio
async def test_skills_resync_requires_portal_header(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    skills = tmp_path / "skills"
    state = tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_SKILLS_DIR", str(skills))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode" / "opencode.json"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient(), start_event_bridge=False)
    client = TestClient(TestServer(app))
    await client.start_server()

    response = await client.post("/api/internal/skills/resync")

    assert response.status == 403
    await client.close()


@pytest.mark.asyncio
async def test_skills_resync_syncs_resources(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    skills = tmp_path / "skills"
    state = tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_SKILLS_DIR", str(skills))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode" / "opencode.json"))
    (skills / "demo" / "scripts").mkdir(parents=True)
    (skills / "demo" / "SKILL.md").write_text("---\nname: demo\ndescription: Demo\n---\n\nBody\n", encoding="utf-8")
    (skills / "demo" / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient(), start_event_bridge=False)
    client = TestClient(TestServer(app))
    await client.start_server()

    response = await client.post("/api/internal/skills/resync", headers={"X-Portal-Author-Source": "portal"})
    body = await response.json()

    assert response.status == 202
    assert body["success"] is True
    assert body["pending_restart"] is True
    assert body["restart_performed"] is False
    assert (workspace / ".opencode" / "skills" / "demo" / "scripts" / "run.py").exists()
    assert body["skills"][0]["resource_files"] == ["scripts/run.py"]
    await client.close()


class FakeManager:
    def __init__(self):
        self.reason = None

    async def start(self, env, *, reason):
        self.reason = reason
        return {"health_ok": True, "pid": 122, "last_restart_reason": reason}

    async def restart(self, env, *, reason):
        self.reason = reason
        return {"health_ok": True, "pid": 123, "last_restart_reason": reason}

    async def stop(self):
        return None


@pytest.mark.asyncio
async def test_skills_resync_restarts_managed_opencode_when_manager_present(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    skills = tmp_path / "skills"
    state = tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_SKILLS_DIR", str(skills))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode" / "opencode.json"))
    (skills / "demo").mkdir(parents=True)
    (skills / "demo" / "SKILL.md").write_text("---\nname: demo\ndescription: Demo\n---\n\nBody\n", encoding="utf-8")
    fake_manager = FakeManager()
    app = create_app(
        Settings.from_env(),
        opencode_client=FakeOpenCodeClient(),
        start_event_bridge=False,
        opencode_process_manager=fake_manager,
    )
    client = TestClient(TestServer(app))
    await client.start_server()

    response = await client.post("/api/internal/skills/resync", headers={"X-Portal-Author-Source": "portal"})
    body = await response.json()

    assert response.status == 200
    assert body["restart_performed"] is True
    assert body["pending_restart"] is False
    assert body["opencode_pid"] == 123
    assert fake_manager.reason == "skills_resync"
    await client.close()
