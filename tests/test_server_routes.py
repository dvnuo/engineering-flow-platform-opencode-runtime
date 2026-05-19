import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.app_keys import CHAT_RUN_STORE_KEY, EVENT_BUS_KEY
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


class _RunStateFakeOpenCodeClient(FakeOpenCodeClient):
    def __init__(self, *, state: str = "running", missing_messages: bool = False):
        super().__init__()
        self.state = state
        self.missing_messages = missing_messages

    async def get_session_status(self):
        return {"sessions": {sid: {"state": self.state} for sid in self.sessions}}

    async def list_messages(self, session_id):
        if self.missing_messages:
            from efp_opencode_adapter.opencode_client import OpenCodeClientError

            raise OpenCodeClientError("not found", status=404)
        return await super().list_messages(session_id)


def test_chat_stream_and_events_routes_still_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())

    routes = {(route.method, route.resource.canonical) for route in app.router.routes()}

    assert ("POST", "/api/chat") in routes
    assert ("POST", "/api/chat/stream") in routes
    assert ("GET", "/api/chat/runs") in routes
    assert ("GET", "/api/chat/runs/{request_id}") in routes
    assert ("POST", "/api/chat/runs/{request_id}/abort") in routes
    assert ("GET", "/api/internal/opencode/status") in routes
    assert ("GET", "/api/internal/opencode/log-tail") in routes
    assert ("GET", "/api/internal/chat/runs/{request_id}/diagnostics") in routes
    assert ("GET", "/api/events") in routes
    assert ("GET", "/api/sessions/{session_id}/active-run") in routes
    assert ("POST", "/api/sessions/{session_id}/abort") in routes


def test_event_bus_uses_replay_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_EVENT_REPLAY_LIMIT", "17")
    monkeypatch.setenv("EFP_EVENT_REPLAY_TTL_SECONDS", "23")

    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    bus = app[EVENT_BUS_KEY]

    assert bus.replay_limit == 17
    assert bus.replay_ttl_seconds == 23


class _StatusManager:
    def __init__(self, log_tail: str = "token=ghp_SECRET\nok") -> None:
        self._log_tail = log_tail

    def status_snapshot(self):
        return {"running": True, "pid": 123, "last_restart_reason": "startup", "last_restart_at": "2026-05-19T00:00:00Z"}

    async def start(self, env=None, reason="startup"):
        return self.status_snapshot()

    async def stop(self):
        return {"running": False}

    def log_tail(self, lines=200):
        return self._log_tail


@pytest.mark.asyncio
async def test_internal_opencode_status_log_tail_and_chat_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient(), opencode_process_manager=_StatusManager())
    app[CHAT_RUN_STORE_KEY].start_run(request_id="req-diag", portal_session_id="sess", opencode_session_id="ses", status="running")
    app[CHAT_RUN_STORE_KEY].record_transport_error(
        "req-diag",
        {"exception_type": "ServerDisconnectedError", "method": "POST", "path": "/session/ses/message", "exception": "ghp_SECRET"},
    )
    client = TestClient(TestServer(app))
    await client.start_server()

    status = await (await client.get("/api/internal/opencode/status")).json()
    assert status["process"]["running"] is True
    assert status["health"]["healthy"] is True

    log_tail = await (await client.get("/api/internal/opencode/log-tail?lines=200")).json()
    assert "ghp_SECRET" not in log_tail["log_tail"]
    assert "***REDACTED***" in log_tail["log_tail"]

    diagnostics = await (await client.get("/api/internal/chat/runs/req-diag/diagnostics")).json()
    assert diagnostics["diagnostics"]["last_transport_error"]["exception_type"] == "ServerDisconnectedError"
    assert "ghp_SECRET" not in str(diagnostics)
    await client.close()


@pytest.mark.asyncio
async def test_active_run_route_validates_against_opencode_active(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _RunStateFakeOpenCodeClient(state="running")
    fake.sessions["ses-active"] = {"id": "ses-active", "title": "Chat"}
    fake.messages["ses-active"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[CHAT_RUN_STORE_KEY].start_run(request_id="req-active", portal_session_id="portal-1", opencode_session_id="ses-active", status="running")
    client = TestClient(TestServer(app))
    await client.start_server()

    payload = await (await client.get("/api/sessions/portal-1/active-run")).json()
    assert payload["run"]["request_id"] == "req-active"
    assert payload["run"]["opencode_active"] is True
    assert payload["run"]["source_of_truth"] == "opencode"

    active_runs = await (await client.get("/api/chat/runs?session_id=portal-1&active=1")).json()
    assert [run["request_id"] for run in active_runs["runs"]] == ["req-active"]
    await client.close()


@pytest.mark.asyncio
async def test_active_run_route_returns_null_when_opencode_inactive_or_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _RunStateFakeOpenCodeClient(state="idle")
    fake.sessions["ses-idle"] = {"id": "ses-idle", "title": "Chat"}
    fake.messages["ses-idle"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[CHAT_RUN_STORE_KEY].start_run(request_id="req-idle", portal_session_id="portal-1", opencode_session_id="ses-idle", status="running")
    client = TestClient(TestServer(app))
    await client.start_server()

    payload = await (await client.get("/api/sessions/portal-1/active-run")).json()
    assert payload["run"] is None
    assert app[CHAT_RUN_STORE_KEY].get("req-idle").status == "stale"

    active_runs = await (await client.get("/api/chat/runs?session_id=portal-1&active=1")).json()
    assert active_runs["runs"] == []
    await client.close()


@pytest.mark.asyncio
async def test_abort_chat_run_marks_terminal_and_publishes_events(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _RunStateFakeOpenCodeClient(state="running")
    fake.sessions["ses-abort"] = {"id": "ses-abort", "title": "Chat"}
    fake.messages["ses-abort"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[CHAT_RUN_STORE_KEY].start_run(request_id="req-abort", portal_session_id="portal-1", opencode_session_id="ses-abort", status="running")
    client = TestClient(TestServer(app))
    await client.start_server()

    payload = await (await client.post("/api/chat/runs/req-abort/abort")).json()
    assert payload["success"] is True
    assert payload["run"]["status"] == "aborted"
    assert fake.abort_tree_calls == ["ses-abort"]
    assert app[CHAT_RUN_STORE_KEY].active_for_session("portal-1") is None
    events = app[EVENT_BUS_KEY].recent_events(request_id="req-abort")
    assert [event["type"] for event in events] == ["chat.run.aborted", "opencode.session.aborted"]
    await client.close()


@pytest.mark.asyncio
async def test_abort_session_marks_latest_run_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _RunStateFakeOpenCodeClient(state="running")
    fake.sessions["ses-abort"] = {"id": "ses-abort", "title": "Chat"}
    fake.messages["ses-abort"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[CHAT_RUN_STORE_KEY].start_run(request_id="req-abort", portal_session_id="portal-1", opencode_session_id="ses-abort", status="running")
    client = TestClient(TestServer(app))
    await client.start_server()

    payload = await (await client.post("/api/sessions/portal-1/abort")).json()
    assert payload["success"] is True
    assert payload["run"]["status"] == "aborted"
    assert fake.abort_tree_calls == ["ses-abort"]
    assert app[CHAT_RUN_STORE_KEY].active_for_session("portal-1") is None
    await client.close()
