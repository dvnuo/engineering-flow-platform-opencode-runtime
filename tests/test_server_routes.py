import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.app_keys import CHAT_RUN_STORE_KEY, EVENT_BRIDGE_KEY, EVENT_BUS_KEY, SESSION_STORE_KEY
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.session_store import SessionRecord
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


class _RunStateFakeOpenCodeClient(FakeOpenCodeClient):
    def __init__(
        self,
        *,
        state: str = "running",
        session_states: dict | None = None,
        children: dict | None = None,
        missing_messages: bool = False,
        abort_result: dict | None = None,
        state_after_abort: str | None = None,
    ):
        super().__init__()
        self.state = state
        self.session_states = session_states or {}
        self.children = children or {}
        self.missing_messages = missing_messages
        self.abort_result = abort_result
        self.state_after_abort = state_after_abort

    async def get_session_status(self):
        return {"sessions": {sid: self.session_states.get(sid, {"state": self.state}) for sid in self.sessions}}

    async def list_session_children(self, session_id):
        if session_id not in self.sessions:
            from efp_opencode_adapter.opencode_client import OpenCodeClientError

            raise OpenCodeClientError("not found", status=404)
        return list(self.children.get(session_id, []))

    async def list_messages(self, session_id):
        if self.missing_messages:
            from efp_opencode_adapter.opencode_client import OpenCodeClientError

            raise OpenCodeClientError("not found", status=404)
        return await super().list_messages(session_id)

    async def abort_session_tree(self, session_id):
        if self.abort_result is not None:
            self.abort_tree_calls.append(session_id)
            result = self.abort_result
        else:
            result = await super().abort_session_tree(session_id)
        if self.state_after_abort is not None:
            self.state = self.state_after_abort
            if session_id in self.session_states:
                self.session_states[session_id] = {"state": self.state_after_abort}
        return result


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
    assert ("GET", "/api/sessions/{session_id}/status") in routes
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


class _EventStreamFakeOpenCodeClient(FakeOpenCodeClient):
    async def event_stream(self, *args, **kwargs):
        if False:
            yield {}


def test_managed_opencode_app_enables_event_bridge_with_injected_client(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(
        Settings.from_env(),
        opencode_client=_EventStreamFakeOpenCodeClient(),
        opencode_process_manager=_StatusManager(),
    )

    assert EVENT_BRIDGE_KEY in app


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
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            portal_session_id="portal-1",
            opencode_session_id="ses-active",
            title="Chat",
            agent=None,
            model=None,
            created_at="2026-05-20T00:00:00Z",
            updated_at="2026-05-20T00:00:00Z",
            last_message="",
            message_count=0,
        )
    )
    app[CHAT_RUN_STORE_KEY].start_run(request_id="req-active", portal_session_id="portal-1", opencode_session_id="ses-active", status="running")
    client = TestClient(TestServer(app))
    await client.start_server()

    payload = await (await client.get("/api/sessions/portal-1/active-run")).json()
    assert payload["success"] is True
    assert payload["active"] is True
    assert payload["active_run"]["request_id"] == "req-active"
    assert payload["active_run"]["opencode_active"] is True
    assert payload["active_run"]["source_of_truth"] == "opencode"
    assert payload["active_run"]["portal_session_id"] == "portal-1"
    assert payload["active_run"]["opencode_session_id"] == "ses-active"
    assert payload["active_run"]["can_abort"] is True
    assert payload["active_run"]["action_hint"] == "wait_reconnect_or_stop"
    assert payload["run"] == payload["active_run"]

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
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            portal_session_id="portal-1",
            opencode_session_id="ses-idle",
            title="Chat",
            agent=None,
            model=None,
            created_at="2026-05-20T00:00:00Z",
            updated_at="2026-05-20T00:00:00Z",
            last_message="",
            message_count=0,
        )
    )
    app[CHAT_RUN_STORE_KEY].start_run(request_id="req-idle", portal_session_id="portal-1", opencode_session_id="ses-idle", status="running")
    client = TestClient(TestServer(app))
    await client.start_server()

    payload = await (await client.get("/api/sessions/portal-1/active-run")).json()
    assert payload["active"] is False
    assert payload["active_run"] is None
    assert payload["run"] is None
    assert payload["source_of_truth"] == "opencode"
    assert payload["diagnostics"]["chat_run_store_latest"]["request_id"] == "req-idle"
    assert payload["diagnostics"]["chat_run_store_stale_count"] == 1
    assert payload["diagnostics"]["chat_run_store_latest"]["status"] == "stale"
    assert payload["action_hint"] == "safe_to_send"
    assert app[CHAT_RUN_STORE_KEY].get("req-idle").status == "stale"

    active_runs = await (await client.get("/api/chat/runs?session_id=portal-1&active=1")).json()
    assert active_runs["runs"] == []
    await client.close()


@pytest.mark.asyncio
async def test_active_run_route_completes_local_run_when_opencode_has_final_assistant(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _RunStateFakeOpenCodeClient(state="idle")
    fake.sessions["ses-done"] = {"id": "ses-done", "title": "Chat"}
    fake.messages["ses-done"] = [{"id": "a-final", "role": "assistant", "parts": [{"type": "text", "text": "done"}], "finish_reason": "stop"}]
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            portal_session_id="portal-1",
            opencode_session_id="ses-done",
            title="Chat",
            agent=None,
            model=None,
            created_at="2026-05-20T00:00:00Z",
            updated_at="2026-05-20T00:00:00Z",
            last_message="",
            message_count=0,
        )
    )
    app[CHAT_RUN_STORE_KEY].start_run(request_id="req-done", portal_session_id="portal-1", opencode_session_id="ses-done", status="running")
    client = TestClient(TestServer(app))
    await client.start_server()

    payload = await (await client.get("/api/sessions/portal-1/active-run")).json()
    assert payload["active"] is False
    assert payload["active_run"] is None
    assert payload["source_of_truth"] == "opencode"
    assert payload["diagnostics"]["chat_run_store_latest"]["request_id"] == "req-done"
    assert payload["diagnostics"]["chat_run_store_latest"]["status"] == "completed"
    assert app[CHAT_RUN_STORE_KEY].get("req-done").status == "completed"
    await client.close()


@pytest.mark.asyncio
async def test_active_run_route_uses_session_binding_when_no_local_run(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _RunStateFakeOpenCodeClient(state="busy")
    fake.sessions["ses-busy"] = {"id": "ses-busy", "title": "Chat"}
    fake.messages["ses-busy"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            portal_session_id="portal-1",
            opencode_session_id="ses-busy",
            title="Chat",
            agent=None,
            model=None,
            created_at="2026-05-20T00:00:00Z",
            updated_at="2026-05-20T00:00:00Z",
            last_message="",
            message_count=0,
        )
    )
    client = TestClient(TestServer(app))
    await client.start_server()

    payload = await (await client.get("/api/sessions/portal-1/active-run")).json()
    assert payload["active"] is True
    assert payload["active_run"]["source_of_truth"] == "opencode"
    assert payload["active_run"]["opencode_active"] is True
    assert payload["active_run"]["opencode_status"] == "busy"
    assert payload["active_run"]["request_id"] == "opencode-session-ses-busy"
    assert payload["active_run"]["can_abort"] is True
    assert payload["action_hint"] == "wait_reconnect_or_stop"
    await client.close()


@pytest.mark.asyncio
async def test_active_run_route_missing_binding_is_safe_to_send(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=_RunStateFakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    payload = await (await client.get("/api/sessions/missing/active-run")).json()
    assert payload["success"] is True
    assert payload["active"] is False
    assert payload["active_run"] is None
    assert payload["reason"] == "missing_session_binding"
    assert payload["action_hint"] == "safe_to_send"
    await client.close()


@pytest.mark.asyncio
async def test_active_run_route_marks_missing_opencode_session_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _RunStateFakeOpenCodeClient(state="running", missing_messages=True)
    fake.sessions["ses-missing"] = {"id": "ses-missing", "title": "Chat"}
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            portal_session_id="portal-1",
            opencode_session_id="ses-missing",
            title="Chat",
            agent=None,
            model=None,
            created_at="2026-05-20T00:00:00Z",
            updated_at="2026-05-20T00:00:00Z",
            last_message="",
            message_count=0,
        )
    )
    app[CHAT_RUN_STORE_KEY].start_run(request_id="req-missing", portal_session_id="portal-1", opencode_session_id="ses-missing", status="running")
    client = TestClient(TestServer(app))
    await client.start_server()

    payload = await (await client.get("/api/sessions/portal-1/active-run")).json()
    assert payload["active"] is False
    assert payload["reason"] == "opencode_session_missing"
    assert payload["source_of_truth"] == "opencode"
    assert payload["diagnostics"]["chat_run_store_stale_count"] == 1
    assert payload["diagnostics"]["chat_run_store_latest"]["status"] == "stale"
    assert app[CHAT_RUN_STORE_KEY].get("req-missing").status == "stale"
    await client.close()


@pytest.mark.asyncio
async def test_active_run_route_reports_child_active_without_locking_root(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _RunStateFakeOpenCodeClient(
        session_states={"root": {"type": "idle"}, "child": {"type": "busy"}},
        children={"root": [{"id": "child"}]},
    )
    fake.sessions["root"] = {"id": "root", "title": "Chat"}
    fake.sessions["child"] = {"id": "child", "title": "Subsession"}
    fake.messages["root"] = []
    fake.messages["child"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            portal_session_id="portal-1",
            opencode_session_id="root",
            title="Chat",
            agent=None,
            model=None,
            created_at="2026-05-20T00:00:00Z",
            updated_at="2026-05-20T00:00:00Z",
            last_message="",
            message_count=0,
        )
    )
    app[CHAT_RUN_STORE_KEY].start_run(request_id="req-child", portal_session_id="portal-1", opencode_session_id="root", status="running")
    client = TestClient(TestServer(app))
    await client.start_server()

    payload = await (await client.get("/api/sessions/portal-1/active-run")).json()
    assert payload["active"] is False
    assert payload["active_run"] is None
    assert payload["reason"] == "active_child_session_non_blocking"
    assert payload["diagnostics"]["active_child_sessions"] == ["child"]
    assert payload["action_hint"] == "safe_to_send"
    assert app[CHAT_RUN_STORE_KEY].get("req-child").status == "stale"
    await client.close()


@pytest.mark.asyncio
async def test_session_status_route_missing_binding_is_safe_to_send(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=_RunStateFakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    payload = await (await client.get("/api/sessions/s-missing/status")).json()

    assert payload["success"] is True
    assert payload["source_of_truth"] == "opencode"
    assert payload["status_type"] == "unknown"
    assert payload["active"] is False
    assert payload["active_run"] is None
    assert payload["can_abort"] is False
    assert payload["action_hint"] == "safe_to_send"
    await client.close()


@pytest.mark.asyncio
async def test_session_status_route_reports_root_busy(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _RunStateFakeOpenCodeClient(session_states={"oc-busy": {"type": "busy"}})
    fake.sessions["oc-busy"] = {"id": "oc-busy", "title": "Chat"}
    fake.messages["oc-busy"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            portal_session_id="portal-busy",
            opencode_session_id="oc-busy",
            title="Chat",
            agent=None,
            model=None,
            created_at="2026-05-20T00:00:00Z",
            updated_at="2026-05-20T00:00:00Z",
            last_message="",
            message_count=0,
        )
    )
    client = TestClient(TestServer(app))
    await client.start_server()

    payload = await (await client.get("/api/sessions/portal-busy/status")).json()

    assert payload["success"] is True
    assert payload["source_of_truth"] == "opencode"
    assert payload["status_type"] == "busy"
    assert payload["active"] is True
    assert payload["can_abort"] is True
    assert payload["action_hint"] == "wait_reconnect_or_stop"
    assert payload["active_run"]["request_id"] == "opencode-session-oc-busy"
    assert payload["active_run"]["session_id"] == "portal-busy"
    assert payload["active_run"]["opencode_session_id"] == "oc-busy"
    assert payload["active_run"]["source_of_truth"] == "opencode"
    assert payload["active_run"]["opencode_active"] is True
    await client.close()


@pytest.mark.asyncio
async def test_session_status_route_reports_child_busy_without_locking_root(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _RunStateFakeOpenCodeClient(
        session_states={"root": {"type": "idle"}, "child": {"type": "busy"}},
        children={"root": [{"id": "child"}]},
    )
    fake.sessions["root"] = {"id": "root", "title": "Chat"}
    fake.sessions["child"] = {"id": "child", "title": "Subsession"}
    fake.messages["root"] = []
    fake.messages["child"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            portal_session_id="portal-root",
            opencode_session_id="root",
            title="Chat",
            agent=None,
            model=None,
            created_at="2026-05-20T00:00:00Z",
            updated_at="2026-05-20T00:00:00Z",
            last_message="",
            message_count=0,
        )
    )
    client = TestClient(TestServer(app))
    await client.start_server()

    payload = await (await client.get("/api/sessions/portal-root/status")).json()

    assert payload["status_type"] == "idle"
    assert payload["active"] is False
    assert payload["active_run"] is None
    assert payload["active_child_sessions"] == ["child"]
    assert payload["diagnostics"]["active_child_sessions"] == ["child"]
    assert payload["action_hint"] == "safe_to_send"
    await client.close()


@pytest.mark.asyncio
async def test_session_status_route_uses_validated_local_active_run(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _RunStateFakeOpenCodeClient(session_states={"oc-busy": {"type": "busy"}})
    fake.sessions["oc-busy"] = {"id": "oc-busy", "title": "Chat"}
    fake.messages["oc-busy"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            portal_session_id="portal-busy",
            opencode_session_id="oc-busy",
            title="Chat",
            agent=None,
            model=None,
            created_at="2026-05-20T00:00:00Z",
            updated_at="2026-05-20T00:00:00Z",
            last_message="",
            message_count=0,
        )
    )
    app[CHAT_RUN_STORE_KEY].start_run(request_id="req-busy", portal_session_id="portal-busy", opencode_session_id="oc-busy", status="running")
    client = TestClient(TestServer(app))
    await client.start_server()

    payload = await (await client.get("/api/sessions/portal-busy/status")).json()

    assert payload["active"] is True
    assert payload["active_run"]["request_id"] == "req-busy"
    assert payload["active_run"]["source_of_truth"] == "opencode"
    assert payload["active_run"]["opencode_active"] is True
    assert payload["active_run"]["can_abort"] is True
    assert payload["active_run"]["action_hint"] == "wait_reconnect_or_stop"
    await client.close()


@pytest.mark.asyncio
async def test_abort_chat_run_marks_terminal_and_publishes_events(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _RunStateFakeOpenCodeClient(state="running", state_after_abort="idle")
    fake.sessions["ses-abort"] = {"id": "ses-abort", "title": "Chat"}
    fake.messages["ses-abort"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            portal_session_id="portal-1",
            opencode_session_id="ses-abort",
            title="Chat",
            agent=None,
            model=None,
            created_at="2026-05-20T00:00:00Z",
            updated_at="2026-05-20T00:00:00Z",
            last_message="",
            message_count=0,
        )
    )
    app[CHAT_RUN_STORE_KEY].start_run(request_id="req-abort", portal_session_id="portal-1", opencode_session_id="ses-abort", status="running")
    client = TestClient(TestServer(app))
    await client.start_server()

    payload = await (await client.post("/api/chat/runs/req-abort/abort")).json()
    assert payload["success"] is True
    assert payload["source_of_truth"] == "opencode"
    assert payload["active"] is False
    assert payload["action_hint"] == "safe_to_send"
    assert payload["run"]["status"] == "aborted"
    assert fake.abort_tree_calls == ["ses-abort"]
    assert app[CHAT_RUN_STORE_KEY].active_for_session("portal-1") is None
    events = app[EVENT_BUS_KEY].recent_events(request_id="req-abort")
    assert [event["type"] for event in events] == ["chat.run.aborted", "opencode.session.aborted"]
    await client.close()


@pytest.mark.asyncio
async def test_abort_session_marks_latest_run_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _RunStateFakeOpenCodeClient(state="running", state_after_abort="idle")
    fake.sessions["ses-abort"] = {"id": "ses-abort", "title": "Chat"}
    fake.messages["ses-abort"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            portal_session_id="portal-1",
            opencode_session_id="ses-abort",
            title="Chat",
            agent=None,
            model=None,
            created_at="2026-05-20T00:00:00Z",
            updated_at="2026-05-20T00:00:00Z",
            last_message="",
            message_count=0,
        )
    )
    app[CHAT_RUN_STORE_KEY].start_run(request_id="req-abort", portal_session_id="portal-1", opencode_session_id="ses-abort", status="running")
    client = TestClient(TestServer(app))
    await client.start_server()

    payload = await (await client.post("/api/sessions/portal-1/abort")).json()
    assert payload["success"] is True
    assert payload["source_of_truth"] == "opencode"
    assert payload["active"] is False
    assert payload["action_hint"] == "safe_to_send"
    assert payload["run"]["status"] == "aborted"
    assert fake.abort_tree_calls == ["ses-abort"]
    assert app[CHAT_RUN_STORE_KEY].active_for_session("portal-1") is None
    await client.close()


@pytest.mark.asyncio
async def test_abort_chat_run_failure_does_not_mark_aborted(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    abort_result = {
        "success": False,
        "errors": [{"session_id": "ses-abort", "error": "abort failed"}],
        "aborted_session_ids": [],
        "missing_session_ids": [],
    }
    fake = _RunStateFakeOpenCodeClient(state="running", abort_result=abort_result)
    fake.sessions["ses-abort"] = {"id": "ses-abort", "title": "Chat"}
    fake.messages["ses-abort"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            portal_session_id="portal-1",
            opencode_session_id="ses-abort",
            title="Chat",
            agent=None,
            model=None,
            created_at="2026-05-20T00:00:00Z",
            updated_at="2026-05-20T00:00:00Z",
            last_message="",
            message_count=0,
        )
    )
    app[CHAT_RUN_STORE_KEY].start_run(request_id="req-abort", portal_session_id="portal-1", opencode_session_id="ses-abort", status="running")
    client = TestClient(TestServer(app))
    await client.start_server()

    response = await client.post("/api/chat/runs/req-abort/abort")
    payload = await response.json()

    assert response.status == 409
    assert payload["success"] is False
    assert payload["error"] == "opencode_abort_failed"
    record = app[CHAT_RUN_STORE_KEY].get("req-abort")
    assert record.status == "running"
    assert record.metadata["abort_failed"] is True
    assert app[CHAT_RUN_STORE_KEY].active_for_session("portal-1").request_id == "req-abort"
    active_payload = await (await client.get("/api/sessions/portal-1/active-run")).json()
    assert active_payload["run"]["opencode_active"] is True
    events = app[EVENT_BUS_KEY].recent_events(request_id="req-abort")
    event_types = [event["type"] for event in events]
    assert "chat.run.abort_failed" in event_types
    assert "opencode.session.abort_failed" in event_types
    assert "opencode.session.aborted" not in event_types
    await client.close()


@pytest.mark.asyncio
async def test_abort_session_failure_does_not_mark_latest_run_aborted(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    abort_result = {
        "success": False,
        "errors": [{"session_id": "ses-abort", "error": "abort failed"}],
        "aborted_session_ids": [],
        "missing_session_ids": [],
    }
    fake = _RunStateFakeOpenCodeClient(state="running", abort_result=abort_result)
    fake.sessions["ses-abort"] = {"id": "ses-abort", "title": "Chat"}
    fake.messages["ses-abort"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            portal_session_id="portal-1",
            opencode_session_id="ses-abort",
            title="Chat",
            agent=None,
            model=None,
            created_at="2026-05-20T00:00:00Z",
            updated_at="2026-05-20T00:00:00Z",
            last_message="",
            message_count=0,
        )
    )
    app[CHAT_RUN_STORE_KEY].start_run(request_id="req-abort", portal_session_id="portal-1", opencode_session_id="ses-abort", status="running")
    client = TestClient(TestServer(app))
    await client.start_server()

    response = await client.post("/api/sessions/portal-1/abort", json={"force_detach": False})
    payload = await response.json()

    assert response.status == 409
    assert payload["success"] is False
    assert payload["error"] == "opencode_abort_failed"
    assert payload["source_of_truth"] == "opencode"
    assert payload["active"] is True
    assert payload["action_hint"] == "hard_reset_or_new_session"
    record = app[CHAT_RUN_STORE_KEY].get("req-abort")
    assert record.status == "running"
    assert record.metadata.get("abort_failed") is not True
    assert app[CHAT_RUN_STORE_KEY].active_for_session("portal-1").request_id == "req-abort"
    await client.close()


@pytest.mark.asyncio
async def test_abort_missing_opencode_session_marks_stale_not_aborted(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    abort_result = {
        "success": True,
        "aborted_session_ids": [],
        "missing_session_ids": ["ses-missing"],
        "errors": [],
    }
    fake = _RunStateFakeOpenCodeClient(state="running", missing_messages=True, abort_result=abort_result)
    fake.sessions["ses-missing"] = {"id": "ses-missing", "title": "Chat"}
    fake.messages["ses-missing"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[CHAT_RUN_STORE_KEY].start_run(request_id="req-missing", portal_session_id="portal-1", opencode_session_id="ses-missing", status="running")
    client = TestClient(TestServer(app))
    await client.start_server()

    response = await client.post("/api/chat/runs/req-missing/abort")
    payload = await response.json()

    assert response.status == 200
    assert payload["success"] is True
    assert payload["stale"] is True
    assert payload["reason"] == "opencode_session_missing_after_abort"
    assert payload["run"]["status"] == "stale"
    assert payload["run"]["incomplete_reason"] == "opencode_session_missing_after_abort"
    assert app[CHAT_RUN_STORE_KEY].active_for_session("portal-1") is None
    event_types = [event["type"] for event in app[EVENT_BUS_KEY].recent_events(request_id="req-missing")]
    assert "chat.run.stale" in event_types
    assert "opencode.session.missing" in event_types
    assert "opencode.session.aborted" not in event_types
    await client.close()
