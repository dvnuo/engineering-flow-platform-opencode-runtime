import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.event_bridge import OpenCodeEventBridge
from efp_opencode_adapter.event_bus import EventBus
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.session_store import SessionRecord, SessionStore
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.state import ensure_state_dirs
from efp_opencode_adapter.task_store import TaskStore


class FakeClient:
    async def health(self): return {"healthy": True}
    async def event_stream(self, **kwargs):
        if False:
            yield {}


@pytest.mark.asyncio
async def test_normalizes_permission_event_and_maps_portal_session(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    settings = Settings.from_env()
    paths = ensure_state_dirs(settings)
    session_store = SessionStore(paths.sessions_dir)
    session_store.upsert(SessionRecord("portal-1", "oc-1", "t", None, None, "a", "a", "", 0))
    task_store = TaskStore(paths.tasks_dir)
    bus = EventBus()
    bridge = OpenCodeEventBridge(settings, FakeClient(), bus, session_store, task_store)
    q = bus.subscribe({"session_id": "portal-1"})
    event = await bridge.publish_raw_event({"payload": {"type": "permission.asked", "properties": {"sessionID": "oc-1", "requestID": "perm-1"}}})
    got = await asyncio.wait_for(q.queue.get(), timeout=1)
    assert got["type"] == "permission_request"
    assert event and event["permission_id"] == "perm-1"


@pytest.mark.asyncio
async def test_normalizes_tool_events(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    settings = Settings.from_env()
    bridge = OpenCodeEventBridge(settings, FakeClient(), EventBus(), SessionStore(ensure_state_dirs(settings).sessions_dir), TaskStore(ensure_state_dirs(settings).tasks_dir))
    s = await bridge.publish_raw_event({"type": "tool.start"})
    c = await bridge.publish_raw_event({"type": "tool.complete"})
    assert s["type"] == "tool.started"
    assert c["type"] == "tool.completed"


def test_create_app_does_not_auto_start_bridge_for_injected_fake_client(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    app = create_app(Settings.from_env(), opencode_client=FakeClient())
    assert "event_bridge" not in app


@pytest.mark.asyncio
async def test_create_app_can_force_start_bridge_for_injected_fake_client(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    app = create_app(Settings.from_env(), opencode_client=FakeClient(), start_event_bridge=True)
    client = TestClient(TestServer(app))
    await client.start_server()
    await client.close()
