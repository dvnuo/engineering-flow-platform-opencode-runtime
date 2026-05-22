import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.session_store import SessionRecord
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.app_keys import SESSION_STORE_KEY
from test_t06_helpers import FakeOpenCodeClient


class StatusClient(FakeOpenCodeClient):
    def __init__(self, state="idle", abort_sets_idle=True):
        super().__init__()
        self.state = state
        self.abort_sets_idle = abort_sets_idle
        self.abort_tree_calls = []

    async def get_session_status(self):
        return {"sessions": {sid: {"state": self.state} for sid in self.sessions}}

    async def abort_session_tree(self, session_id):
        self.abort_tree_calls.append(session_id)
        if self.abort_sets_idle:
            self.state = "idle"
        return {"success": True, "supported": True, "aborted_session_ids": [session_id], "missing_session_ids": [], "errors": []}


def test_long_task_routes_are_not_registered(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    routes = {(route.method, route.resource.canonical) for route in app.router.routes()}

    assert ("POST", "/api/chat") in routes
    assert ("POST", "/api/chat/stream") in routes
    assert ("GET", "/api/chat/runs") not in routes
    assert ("GET", "/api/chat/runs/{request_id}") not in routes
    assert ("POST", "/api/chat/runs/{request_id}/abort") not in routes
    assert ("GET", "/api/sessions/{session_id}/active-run") not in routes
    assert ("POST", "/api/sessions/{session_id}/hard-reset") not in routes
    assert ("GET", "/api/sessions/{session_id}/status") in routes
    assert ("POST", "/api/sessions/{session_id}/abort") in routes


@pytest.mark.asyncio
async def test_session_status_and_abort_still_work(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = StatusClient(state="busy", abort_sets_idle=True)
    fake.sessions["oc-1"] = {"id": "oc-1", "title": "Chat"}
    fake.messages["oc-1"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(SessionRecord("portal-1", "oc-1", "Chat", None, None, "a", "b", "", 0))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        status = await (await client.get("/api/sessions/portal-1/status")).json()
        assert status["active"] is True
        assert status["status"]["type"] == "busy"

        abort = await client.post("/api/sessions/portal-1/abort")
        abort_payload = await abort.json()
        assert abort.status == 200
        assert abort_payload["active"] is False
        assert abort_payload["status"]["type"] == "idle"
        assert fake.abort_tree_calls == ["oc-1"]
    finally:
        await client.close()
