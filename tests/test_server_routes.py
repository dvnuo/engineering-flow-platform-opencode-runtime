import logging

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter import server as server_mod
from efp_opencode_adapter.app_keys import CHATLOG_STORE_KEY, SESSION_STORE_KEY
from efp_opencode_adapter.server import create_app, flush_chatlog_store
from efp_opencode_adapter.session_store import SessionRecord
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


def test_chat_run_recovery_routes_are_registered_without_removed_long_run_controls(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    routes = {(route.method, route.resource.canonical) for route in app.router.routes()}

    assert ("POST", "/api/chat") in routes
    assert ("POST", "/api/chat/stream") in routes
    assert ("GET", "/api/chat/runs") not in routes
    assert ("GET", "/api/chat/runs/{request_id}") in routes
    assert ("POST", "/api/chat/runs/{request_id}/cancel") in routes
    assert ("POST", "/api/chat/runs/{request_id}/abort") not in routes
    assert ("GET", "/api/sessions/{session_id}/active-run") not in routes
    assert ("POST", "/api/sessions/{session_id}/abort") not in routes
    assert ("POST", "/api/sessions/{session_id}/hard-reset") not in routes
    assert ("GET", "/api/sessions/{session_id}/status") in routes
    assert ("*", "/api/internal/copilot/{tail}") in routes


@pytest.mark.asyncio
async def test_session_status_returns_plain_session_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = FakeOpenCodeClient()
    fake.sessions["oc-1"] = {"id": "oc-1", "title": "Chat", "status": "idle"}
    fake.messages["oc-1"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(SessionRecord("portal-1", "oc-1", "Chat", None, None, "a", "b", "", 0))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get("/api/sessions/portal-1/status")
        payload = await response.json()

        assert response.status == 200
        assert payload["success"] is True
        assert payload["status"]["type"] == "idle"
        assert payload["exists"] is True
        assert "active_run" not in payload
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_shutdown_lands_coalesced_chatlog_events_on_disk(tmp_path, monkeypatch):
    """Coalesced runtime-event appends must not be lost when the pod stops."""
    import json

    from efp_opencode_adapter.app_keys import CHATLOG_STORE_KEY

    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    store = app[CHATLOG_STORE_KEY]
    store.event_flush_interval_seconds = 3600.0  # nothing flushes on its own here
    chatlog_path = tmp_path / "state" / "chatlogs" / "portal-1.json"

    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        store.start_entry("portal-1", request_id="r1", message="hello")
        store.append_event("portal-1", request_id="r1", event={"type": "tool.started"})
        on_disk = json.loads(chatlog_path.read_text(encoding="utf-8"))
        assert on_disk["entries"][-1]["runtime_events"] == []
    finally:
        await client.close()

    on_disk = json.loads(chatlog_path.read_text(encoding="utf-8"))
    assert [e["type"] for e in on_disk["entries"][-1]["runtime_events"]] == ["tool.started"]


@pytest.mark.asyncio
async def test_chatlog_cleanup_offloads_flush_and_warns_on_failure(
    monkeypatch,
    caplog,
):
    calls = []

    class FailingStore:
        def flush_all(self):
            calls.append("flush_all")
            raise OSError("disk unavailable")

    async def fake_to_thread(function, *args):
        calls.append("to_thread")
        return function(*args)

    monkeypatch.setattr(server_mod.asyncio, "to_thread", fake_to_thread)
    app = {CHATLOG_STORE_KEY: FailingStore()}

    with caplog.at_level(logging.WARNING, logger=server_mod.__name__):
        await flush_chatlog_store(app)

    assert calls == ["to_thread", "flush_all"]
    assert "chatlog.flush_all.failed" in caplog.text


def test_chatlog_flush_runs_after_event_bridge_cleanup(tmp_path, monkeypatch):
    class EventStreamClient(FakeOpenCodeClient):
        async def event_stream(self):
            if False:
                yield {}

    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    app = create_app(
        Settings.from_env(),
        opencode_client=EventStreamClient(),
        start_event_bridge=True,
    )

    cleanup_names = [callback.__name__ for callback in app.on_cleanup]

    assert cleanup_names.index("_cleanup_event_bridge") < cleanup_names.index(
        "flush_chatlog_store"
    )
