import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.app_keys import SESSION_STORE_KEY
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.session_store import SessionRecord
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


def test_long_run_routes_are_not_registered(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    routes = {(route.method, route.resource.canonical) for route in app.router.routes()}

    assert ("POST", "/api/chat") in routes
    assert ("POST", "/api/chat/stream") in routes
    assert ("GET", "/api/chat/runs") not in routes
    assert ("GET", "/api/chat/runs/{request_id}") not in routes
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
