import asyncio
import json
from copy import deepcopy

import pytest
from aiohttp.test_utils import TestClient, TestServer

import efp_opencode_adapter.server as server_mod
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.sessions_api import _extract_opencode_session_id, _to_efp_messages
from efp_opencode_adapter.opencode_client import OpenCodeClientError
from efp_opencode_adapter.app_keys import CHAT_RUN_STORE_KEY, EVENT_BUS_KEY, SESSION_STORE_KEY, PORTAL_METADATA_CLIENT_KEY, CHATLOG_STORE_KEY, TASK_BACKGROUND_TASKS_KEY
from efp_opencode_adapter.session_store import SessionRecord
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


class _RunStateFakeOpenCodeClient(FakeOpenCodeClient):
    def __init__(self, *, state: str = "running"):
        super().__init__()
        self.state = state

    async def get_session_status(self):
        return {"sessions": {sid: {"state": self.state} for sid in self.sessions}}


class _AbortableRunStateFakeOpenCodeClient(_RunStateFakeOpenCodeClient):
    def __init__(self, *, state: str = "busy"):
        super().__init__(state=state)
        self.abort_session_tree_calls: list[str] = []

    async def abort_session_tree(self, session_id):
        self.abort_session_tree_calls.append(session_id)
        self.state = "idle"
        return {"success": True, "supported": True, "aborted_session_ids": [session_id], "missing_session_ids": [], "errors": []}


class _StickyAbortRunStateFakeOpenCodeClient(_RunStateFakeOpenCodeClient):
    def __init__(self, *, state: str = "busy"):
        super().__init__(state=state)
        self.abort_session_tree_calls: list[str] = []

    async def abort_session_tree(self, session_id):
        self.abort_session_tree_calls.append(session_id)
        return {"success": True, "supported": True, "aborted_session_ids": [session_id], "missing_session_ids": [], "errors": []}


class _ChildActiveRunStateFakeOpenCodeClient(_RunStateFakeOpenCodeClient):
    async def get_session_status(self):
        return {"sessions": {"root": {"state": "idle"}, "child": {"state": "busy"}}}

    async def list_session_children(self, session_id):
        return [{"id": "child"}] if session_id == "root" else []


def _role_content_pairs(messages):
    return [(message["role"], message["content"]) for message in messages]


async def _fast_wait_until_opencode_inactive(client, opencode_session_id, **_kwargs):
    return await server_mod.resolve_opencode_run_state(client, opencode_session_id)


@pytest.mark.asyncio
async def test_sessions_endpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    cr = await client.post("/api/chat", json={"message": "hello"})
    cp = await cr.json()
    sid = cp["session_id"]

    ls = await client.get("/api/sessions")
    lsp = await ls.json()
    assert len(lsp["sessions"]) == 1
    assert lsp["sessions"][0]["engine"] == "opencode"
    assert lsp["sessions"][0]["message_count"] >= 2

    dt = await client.get(f"/api/sessions/{sid}")
    dp = await dt.json()
    roles = [m["role"] for m in dp["messages"]]
    assert "user" in roles and "assistant" in roles
    assert dp["metadata"]["engine"] == "opencode"

    ch = await client.get(f"/api/sessions/{sid}/chatlog")
    body = await ch.json()
    assert body["success"] is True
    assert "chatlog" in body
    assert "runtime_events" in body
    assert "events" in body

    rn = await client.post(f"/api/sessions/{sid}/rename", json={"name": "renamed"})
    assert (await rn.json())["success"] is True
    ls2 = await client.get("/api/sessions")
    assert (await ls2.json())["sessions"][0]["name"] == "renamed"

    missing = await client.post(f"/api/sessions/{sid}/messages/missing/delete-from-here", json={})
    assert missing.status == 404

    dl = await client.delete(f"/api/sessions/{sid}")
    assert (await dl.json())["success"] is True
    assert (await (await client.get("/api/sessions")).json())["sessions"] == []
    assert (await client.get(f"/api/sessions/{sid}")).status == 404

    await client.post("/api/chat", json={"message": "a", "session_id": "s1"})
    await client.post("/api/chat", json={"message": "b", "session_id": "s2"})
    cl = await client.post("/api/clear")
    assert (await cl.json())["success"] is True
    assert (await (await client.get("/api/sessions")).json())["sessions"] == []
    await client.close()


@pytest.mark.asyncio
async def test_get_session_exposes_canonical_opencode_messages_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = FakeOpenCodeClient()
    fake.sessions["oc-canonical"] = {"id": "oc-canonical", "title": "Chat"}
    fake.messages["oc-canonical"] = [
        {
            "info": {"id": "msg_user_1", "role": "user", "time": {"created": 1710000000000}},
            "parts": [{"id": "part_user_1", "type": "text", "text": "hi"}],
        },
        {
            "info": {"id": "efp-auto-continue-1", "role": "user"},
            "parts": [{"id": "part_internal_1", "type": "text", "text": "continue"}],
        },
        {
            "info": {
                "id": "msg_asst_1",
                "role": "assistant",
                "time": {"created": 1710000001000, "updated": 1710000002000, "completed": 1710000003000},
                "finishReason": "stop",
            },
            "parts": [
                {"id": "part_reason_1", "type": "reasoning", "text": "plan"},
                {"id": "part_tool_1", "type": "tool", "tool": "bash", "state": {"status": "completed"}},
                {"id": "part_text_1", "type": "text", "text": "answer"},
            ],
        },
    ]
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            portal_session_id="portal-canonical",
            opencode_session_id="oc-canonical",
            title="Chat",
            agent=None,
            model=None,
            created_at="2026-05-20T00:00:00Z",
            updated_at="2026-05-20T00:00:00Z",
            last_message="answer",
            message_count=2,
        )
    )
    app[CHAT_RUN_STORE_KEY].start_run(
        request_id="req-local-projection",
        portal_session_id="portal-canonical",
        opencode_session_id="oc-canonical",
        status="running",
    )
    app[CHAT_RUN_STORE_KEY].complete_run(
        "req-local-projection",
        {
            "completion_state": "completed",
            "response": "local projection should not become canonical",
            "assistant_message_id": "local_assistant_only",
            "assistant_message_ids": ["local_assistant_only"],
        },
    )
    client = TestClient(TestServer(app))
    await client.start_server()

    payload = await (await client.get("/api/sessions/portal-canonical")).json()

    assert payload["source_of_truth"] == "opencode"
    assert payload["opencode_session_id"] == "oc-canonical"
    assert payload["metadata"]["source_of_truth"] == "opencode"
    assert payload["messages"]
    assert payload["canonical_messages"]
    assert len(payload["canonical_messages"]) == 2
    assert payload["canonical_messages"][0]["info"]["id"] == "msg_user_1"
    assert payload["canonical_messages"][0]["message_id"] == "msg_user_1"
    assert payload["canonical_messages"][0]["created_at"] == 1710000000000
    assert payload["canonical_messages"][1]["role"] == "assistant"
    assert payload["canonical_messages"][1]["created_at"] == 1710000001000
    assert payload["canonical_messages"][1]["updated_at"] == 1710000002000
    assert payload["canonical_messages"][1]["completed_at"] == 1710000003000
    assert payload["canonical_messages"][1]["finish_reason"] == "stop"
    part_ids = {part["id"] for part in payload["canonical_messages"][1]["parts"]}
    assert {"part_reason_1", "part_tool_1", "part_text_1"} <= part_ids
    encoded_canonical = json.dumps(payload["canonical_messages"])
    assert "efp-auto-continue-1" not in encoded_canonical
    assert "part_internal_1" not in encoded_canonical
    assert "local_assistant_only" not in encoded_canonical
    assert "local projection should not become canonical" not in encoded_canonical
    await client.close()


@pytest.mark.asyncio
async def test_session_metadata_includes_active_latest_run_and_projection(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await client.post("/api/chat", json={"message": "hello", "session_id": "s-runs", "request_id": "r-done"})
        record = app[SESSION_STORE_KEY].get("s-runs")
        app[CHAT_RUN_STORE_KEY].start_run(
            request_id="r-active",
            portal_session_id="s-runs",
            opencode_session_id=record.opencode_session_id,
            assistant_message_id="a-pending",
            status="running",
            stream_state="detached",
        )
        app[CHAT_RUN_STORE_KEY].update_assistant_projection("r-active", text="partial live answer", assistant_message_id="a-pending", display_blocks=[{"type": "text", "text": "partial live answer"}])

        response = await client.get("/api/sessions/s-runs")
        payload = await response.json()
        metadata = payload["metadata"]

        assert metadata["active_run"]["request_id"] == "r-active"
        assert metadata["active_run"]["status"] == "running"
        assert metadata["active_run"]["source_of_truth"] == "opencode"
        assert metadata["active_run"]["opencode_active"] is True
        assert metadata["active_run"]["can_abort"] is True
        assert metadata["active_run"]["action_hint"] == "wait_reconnect_or_stop"
        assert metadata["latest_run"]["request_id"] == "r-active"
        assert metadata["session_status"]["active"] is True
        assert metadata["session_status"]["active_run"]["request_id"] == "r-active"
        assert metadata["session_status"]["action_hint"] == "wait_reconnect_or_stop"
        assert metadata["assistant_projection"]["request_id"] == "r-active"
        assert metadata["assistant_projection"]["assistant_message_id"] == "a-pending"
        assert metadata["assistant_projection"]["text"] == "partial live answer"

        active_response = await client.get("/api/sessions/s-runs/active-run")
        active_payload = await active_response.json()
        assert active_payload["run"]["request_id"] == "r-active"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_session_metadata_validates_active_run_against_opencode(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _RunStateFakeOpenCodeClient(state="running")
    fake.sessions["ses-active"] = {"id": "ses-active", "title": "Chat"}
    fake.messages["ses-active"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            portal_session_id="portal-active",
            opencode_session_id="ses-active",
            title="Chat",
            agent=None,
            model=None,
            created_at="2026-05-19T00:00:00Z",
            updated_at="2026-05-19T00:00:00Z",
            last_message="",
            message_count=0,
        )
    )
    app[CHAT_RUN_STORE_KEY].start_run(request_id="req-active", portal_session_id="portal-active", opencode_session_id="ses-active", status="running")
    client = TestClient(TestServer(app))
    await client.start_server()

    metadata = (await (await client.get("/api/sessions/portal-active")).json())["metadata"]
    assert metadata["active_run"]["request_id"] == "req-active"
    assert metadata["active_run"]["opencode_active"] is True
    assert metadata["session_status"]["active"] is True
    assert metadata["session_status"]["active_run"]["request_id"] == "req-active"
    await client.close()


@pytest.mark.asyncio
async def test_session_metadata_synthesizes_active_run_from_opencode_status(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _RunStateFakeOpenCodeClient(state="busy")
    fake.sessions["ses-busy"] = {"id": "ses-busy", "title": "Chat"}
    fake.messages["ses-busy"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            portal_session_id="portal-busy",
            opencode_session_id="ses-busy",
            title="Chat",
            agent=None,
            model=None,
            created_at="2026-05-19T00:00:00Z",
            updated_at="2026-05-19T00:00:00Z",
            last_message="",
            message_count=0,
        )
    )
    client = TestClient(TestServer(app))
    await client.start_server()

    payload = await (await client.get("/api/sessions/portal-busy")).json()
    metadata = payload["metadata"]

    assert metadata["active_run"]["request_id"] == "opencode-session-ses-busy"
    assert metadata["active_run"]["source_of_truth"] == "opencode"
    assert metadata["active_run"]["opencode_active"] is True
    assert metadata["session_status"]["active"] is True
    assert metadata["session_status"]["active_run"]["request_id"] == "opencode-session-ses-busy"
    assert metadata["session_status"]["can_abort"] is True
    await client.close()


@pytest.mark.asyncio
async def test_session_abort_works_without_chat_run_store_active_record(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _AbortableRunStateFakeOpenCodeClient(state="busy")
    fake.sessions["ses-abort-synthetic"] = {"id": "ses-abort-synthetic", "title": "Chat"}
    fake.messages["ses-abort-synthetic"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            portal_session_id="portal-abort-synthetic",
            opencode_session_id="ses-abort-synthetic",
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

    before = await (await client.get("/api/sessions/portal-abort-synthetic/status")).json()
    assert before["active"] is True
    assert before["active_run"]["request_id"].startswith("opencode-session-")
    assert before["can_abort"] is True

    abort_payload = await (await client.post("/api/sessions/portal-abort-synthetic/abort")).json()
    assert fake.abort_session_tree_calls == ["ses-abort-synthetic"]
    assert abort_payload["success"] is True
    assert abort_payload["engine"] == "opencode"
    assert abort_payload["session_id"] == "portal-abort-synthetic"
    assert abort_payload["opencode_session_id"] == "ses-abort-synthetic"
    assert abort_payload["aborted"] is True
    assert abort_payload["action_hint"] == "safe_to_send"
    assert abort_payload["status"]["type"] in {"idle", "aborted"}
    assert abort_payload["run"] is None

    after = await (await client.get("/api/sessions/portal-abort-synthetic/status")).json()
    assert after["active"] is False
    assert after["active_run"] is None
    assert after["action_hint"] == "safe_to_send"
    assert after["can_abort"] is False
    await client.close()


@pytest.mark.asyncio
async def test_session_abort_publishes_session_aborted_for_synthetic_run(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _AbortableRunStateFakeOpenCodeClient(state="busy")
    fake.sessions["ses-abort-event"] = {"id": "ses-abort-event", "title": "Chat"}
    fake.messages["ses-abort-event"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            portal_session_id="portal-abort-event",
            opencode_session_id="ses-abort-event",
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

    abort_payload = await (await client.post("/api/sessions/portal-abort-event/abort")).json()

    assert fake.abort_session_tree_calls == ["ses-abort-event"]
    assert abort_payload["success"] is True
    assert abort_payload["aborted"] is True
    assert abort_payload["action_hint"] == "safe_to_send"
    assert abort_payload["run"] is None

    events = app[EVENT_BUS_KEY].recent_events(session_id="portal-abort-event")
    event_types = [event["type"] for event in events]
    assert "opencode.session.aborted" in event_types
    assert "chat.run.aborted" not in event_types
    aborted_event = next(event for event in events if event["type"] == "opencode.session.aborted")
    assert aborted_event["session_id"] == "portal-abort-event"
    assert aborted_event["request_id"] == ""
    assert aborted_event["opencode_session_id"] == "ses-abort-event"

    status_payload = await (await client.get("/api/sessions/portal-abort-event/status")).json()
    assert status_payload["active"] is False
    assert status_payload["active_run"] is None
    assert status_payload["can_abort"] is False
    assert status_payload["action_hint"] == "safe_to_send"
    await client.close()


@pytest.mark.asyncio
async def test_session_abort_204_still_busy_force_detach_false_returns_conflict(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(server_mod, "_wait_until_opencode_inactive", _fast_wait_until_opencode_inactive)
    fake = _StickyAbortRunStateFakeOpenCodeClient(state="busy")
    fake.sessions["ses-still-busy"] = {"id": "ses-still-busy", "title": "Chat"}
    fake.messages["ses-still-busy"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            portal_session_id="portal-still-busy",
            opencode_session_id="ses-still-busy",
            title="Chat",
            agent=None,
            model=None,
            created_at="2026-05-20T00:00:00Z",
            updated_at="2026-05-20T00:00:00Z",
            last_message="",
            message_count=0,
        )
    )
    app[CHAT_RUN_STORE_KEY].start_run(request_id="req-still-busy", portal_session_id="portal-still-busy", opencode_session_id="ses-still-busy", status="running")
    client = TestClient(TestServer(app))
    await client.start_server()

    response = await client.post("/api/sessions/portal-still-busy/abort", json={"force_detach": False})
    payload = await response.json()

    assert response.status == 409
    assert payload["success"] is False
    assert payload["error"] == "opencode_abort_still_active"
    assert payload["active"] is True
    assert payload["action_hint"] == "hard_reset_or_new_session"
    assert app[CHAT_RUN_STORE_KEY].get("req-still-busy").status == "running"
    await client.close()


@pytest.mark.asyncio
async def test_session_abort_204_still_busy_force_detach_rebinds_fresh_session(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(server_mod, "_wait_until_opencode_inactive", _fast_wait_until_opencode_inactive)
    fake = _StickyAbortRunStateFakeOpenCodeClient(state="busy")
    fake.sessions["ses-old-busy"] = {"id": "ses-old-busy", "title": "Chat"}
    fake.messages["ses-old-busy"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            portal_session_id="portal-detach",
            opencode_session_id="ses-old-busy",
            title="Chat",
            agent=None,
            model=None,
            created_at="2026-05-20T00:00:00Z",
            updated_at="2026-05-20T00:00:00Z",
            last_message="",
            message_count=3,
        )
    )
    app[CHAT_RUN_STORE_KEY].start_run(request_id="req-detach", portal_session_id="portal-detach", opencode_session_id="ses-old-busy", status="running")
    client = TestClient(TestServer(app))
    await client.start_server()

    payload = await (await client.post("/api/sessions/portal-detach/abort", json={"force_detach": True})).json()

    assert payload["success"] is True
    assert payload["detached_old_session"] is True
    assert payload["old_opencode_session_id"] == "ses-old-busy"
    assert payload["opencode_session_id"] != "ses-old-busy"
    assert payload["active"] is False
    assert payload["action_hint"] == "safe_to_send"
    assert app[SESSION_STORE_KEY].get("portal-detach").opencode_session_id == payload["opencode_session_id"]
    stale = app[CHAT_RUN_STORE_KEY].get("req-detach")
    assert stale.status == "stale"
    assert stale.incomplete_reason == "opencode_abort_still_active_detached"
    await client.close()


@pytest.mark.asyncio
async def test_session_hard_reset_endpoint_rebinds_without_chat_run_store_active_source(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _StickyAbortRunStateFakeOpenCodeClient(state="busy")
    fake.sessions["ses-hard-old"] = {"id": "ses-hard-old", "title": "Chat"}
    fake.messages["ses-hard-old"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            portal_session_id="portal-hard-reset",
            opencode_session_id="ses-hard-old",
            title="Chat",
            agent=None,
            model=None,
            created_at="2026-05-20T00:00:00Z",
            updated_at="2026-05-20T00:00:00Z",
            last_message="",
            message_count=4,
        )
    )
    app[CHAT_RUN_STORE_KEY].start_run(request_id="req-hard", portal_session_id="portal-hard-reset", opencode_session_id="ses-hard-old", status="running")
    client = TestClient(TestServer(app))
    await client.start_server()

    payload = await (await client.post("/api/sessions/portal-hard-reset/hard-reset")).json()

    assert payload["success"] is True
    assert payload["detached_old_session"] is True
    assert payload["old_opencode_session_id"] == "ses-hard-old"
    assert payload["opencode_session_id"] != "ses-hard-old"
    assert payload["active"] is False
    assert payload["action_hint"] == "safe_to_send"
    assert app[SESSION_STORE_KEY].get("portal-hard-reset").opencode_session_id == payload["opencode_session_id"]
    assert app[CHAT_RUN_STORE_KEY].get("req-hard").status == "stale"
    await client.close()


@pytest.mark.asyncio
async def test_session_status_and_active_run_consistent_for_synthetic_active_run(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _RunStateFakeOpenCodeClient(state="busy")
    fake.sessions["ses-synthetic"] = {"id": "ses-synthetic", "title": "Chat"}
    fake.messages["ses-synthetic"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            portal_session_id="portal-synthetic",
            opencode_session_id="ses-synthetic",
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

    status_payload = await (await client.get("/api/sessions/portal-synthetic/status")).json()
    active_payload = await (await client.get("/api/sessions/portal-synthetic/active-run")).json()

    assert status_payload["active"] is True
    assert status_payload["active_run"]["source_of_truth"] == "opencode"
    assert status_payload["active_run"]["opencode_active"] is True
    assert status_payload["action_hint"] == "wait_reconnect_or_stop"
    assert active_payload["active"] is True
    assert active_payload["active_run"]["source_of_truth"] == "opencode"
    assert active_payload["active_run"]["opencode_active"] is True
    assert active_payload["run"] == active_payload["active_run"]
    assert active_payload["action_hint"] == "wait_reconnect_or_stop"
    await client.close()


@pytest.mark.asyncio
async def test_active_run_opencode_idle_overrides_chat_run_store_running(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _RunStateFakeOpenCodeClient(state="idle")
    fake.sessions["ses-idle-active-run"] = {"id": "ses-idle-active-run", "title": "Chat"}
    fake.messages["ses-idle-active-run"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            portal_session_id="portal-idle-active-run",
            opencode_session_id="ses-idle-active-run",
            title="Chat",
            agent=None,
            model=None,
            created_at="2026-05-20T00:00:00Z",
            updated_at="2026-05-20T00:00:00Z",
            last_message="",
            message_count=0,
        )
    )
    app[CHAT_RUN_STORE_KEY].start_run(request_id="req-stale", portal_session_id="portal-idle-active-run", opencode_session_id="ses-idle-active-run", status="running")
    client = TestClient(TestServer(app))
    await client.start_server()

    payload = await (await client.get("/api/sessions/portal-idle-active-run/active-run")).json()

    assert payload["source_of_truth"] == "opencode"
    assert payload["active"] is False
    assert payload["active_run"] is None
    assert payload["run"] is None
    assert payload["action_hint"] == "safe_to_send"
    assert payload["diagnostics"]["chat_run_store_stale_count"] == 1
    assert app[CHAT_RUN_STORE_KEY].get("req-stale").status == "stale"
    await client.close()


@pytest.mark.asyncio
async def test_active_run_opencode_busy_overrides_empty_chat_run_store(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _RunStateFakeOpenCodeClient(state="busy")
    fake.sessions["ses-busy-active-run"] = {"id": "ses-busy-active-run", "title": "Chat"}
    fake.messages["ses-busy-active-run"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            portal_session_id="portal-busy-active-run",
            opencode_session_id="ses-busy-active-run",
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

    payload = await (await client.get("/api/sessions/portal-busy-active-run/active-run")).json()

    assert payload["source_of_truth"] == "opencode"
    assert payload["active"] is True
    assert payload["active_run"]["source_of_truth"] == "opencode"
    assert payload["active_run"]["opencode_active"] is True
    assert payload["active_run"]["can_abort"] is True
    assert payload["action_hint"] == "wait_reconnect_or_stop"
    await client.close()


@pytest.mark.asyncio
async def test_session_status_opencode_idle_overrides_chat_run_store_running(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _RunStateFakeOpenCodeClient(state="idle")
    fake.sessions["ses-idle-status"] = {"id": "ses-idle-status", "title": "Chat"}
    fake.messages["ses-idle-status"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            portal_session_id="portal-idle-status",
            opencode_session_id="ses-idle-status",
            title="Chat",
            agent=None,
            model=None,
            created_at="2026-05-20T00:00:00Z",
            updated_at="2026-05-20T00:00:00Z",
            last_message="",
            message_count=0,
        )
    )
    app[CHAT_RUN_STORE_KEY].start_run(request_id="req-status-stale", portal_session_id="portal-idle-status", opencode_session_id="ses-idle-status", status="running")
    client = TestClient(TestServer(app))
    await client.start_server()

    payload = await (await client.get("/api/sessions/portal-idle-status/status")).json()

    assert payload["source_of_truth"] == "opencode"
    assert payload["active"] is False
    assert payload["can_abort"] is False
    assert payload["status"]["type"] == "idle"
    assert payload["active_run"] is None
    assert payload["action_hint"] == "safe_to_send"
    assert payload["diagnostics"]["chat_run_store_stale_count"] == 1
    assert app[CHAT_RUN_STORE_KEY].get("req-status-stale").status == "stale"
    await client.close()


@pytest.mark.asyncio
async def test_child_active_session_does_not_block_root_active_run(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _ChildActiveRunStateFakeOpenCodeClient()
    fake.sessions["root"] = {"id": "root", "title": "Chat"}
    fake.sessions["child"] = {"id": "child", "title": "Child"}
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

    payload = await (await client.get("/api/sessions/portal-root/active-run")).json()

    assert payload["active"] is False
    assert payload["reason"] == "active_child_session_non_blocking"
    assert payload["active_child_sessions"] == ["child"]
    assert payload["action_hint"] == "safe_to_send"
    await client.close()


@pytest.mark.asyncio
async def test_session_metadata_clears_inactive_local_run_and_keeps_projection(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _RunStateFakeOpenCodeClient(state="idle")
    fake.sessions["ses-idle"] = {"id": "ses-idle", "title": "Chat"}
    fake.messages["ses-idle"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            portal_session_id="portal-idle",
            opencode_session_id="ses-idle",
            title="Chat",
            agent=None,
            model=None,
            created_at="2026-05-19T00:00:00Z",
            updated_at="2026-05-19T00:00:00Z",
            last_message="",
            message_count=0,
        )
    )
    app[CHAT_RUN_STORE_KEY].start_run(request_id="req-idle", portal_session_id="portal-idle", opencode_session_id="ses-idle", status="running")
    app[CHAT_RUN_STORE_KEY].update_assistant_projection("req-idle", text="partial answer", assistant_message_id="a-local")
    client = TestClient(TestServer(app))
    await client.start_server()

    metadata = (await (await client.get("/api/sessions/portal-idle")).json())["metadata"]
    assert metadata["active_run"] is None
    assert metadata["active_run_stale_reason"] == "opencode_not_active"
    assert metadata["latest_run"]["status"] == "stale"
    assert metadata["session_status"]["active"] is False
    assert metadata["session_status"]["active_run"] is None
    assert metadata["session_status"]["action_hint"] == "safe_to_send"
    assert metadata["assistant_projection"]["text"] == "partial answer"
    assert app[CHAT_RUN_STORE_KEY].get("req-idle").status == "stale"
    await client.close()


@pytest.mark.asyncio
async def test_delete_session_clears_chat_run_store_and_aborts_active_run(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = FakeOpenCodeClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        created = await (await client.post("/api/chat", json={"message": "hello", "session_id": "s-delete"})).json()
        record = app[SESSION_STORE_KEY].get("s-delete")
        app[CHAT_RUN_STORE_KEY].start_run(request_id="req-delete-active", portal_session_id="s-delete", opencode_session_id=record.opencode_session_id, status="running")

        payload = await (await client.delete("/api/sessions/s-delete")).json()

        assert payload["success"] is True
        assert payload["chat_runs_deleted"] >= 1
        assert fake.abort_tree_calls == [record.opencode_session_id]
        assert app[CHAT_RUN_STORE_KEY].active_for_session("s-delete") is None
        assert app[CHAT_RUN_STORE_KEY].list_for_session("s-delete") == []
        assert created["session_id"] == "s-delete"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_clear_sessions_clears_chat_run_store(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = FakeOpenCodeClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await client.post("/api/chat", json={"message": "a", "session_id": "s-clear"})
        record = app[SESSION_STORE_KEY].get("s-clear")
        app[CHAT_RUN_STORE_KEY].start_run(request_id="req-clear-active", portal_session_id="s-clear", opencode_session_id=record.opencode_session_id, status="running")

        payload = await (await client.post("/api/clear")).json()

        assert payload["success"] is True
        assert fake.abort_tree_calls == [record.opencode_session_id]
        assert app[CHAT_RUN_STORE_KEY].list_for_session("s-clear") == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_rename_invalid_json_returns_400_json(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    await client.post("/api/chat", json={"message": "hello", "session_id": "s1"})
    res = await client.post("/api/sessions/s1/rename", data='{"name":', headers={"Content-Type": "application/json"})
    payload = await res.json()

    assert res.status == 400
    assert payload["error"] == "invalid_json"
    await client.close()


@pytest.mark.asyncio
async def test_rename_payload_must_be_object(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    await client.post("/api/chat", json={"message": "hello", "session_id": "s1"})
    res = await client.post("/api/sessions/s1/rename", json=["bad"])
    payload = await res.json()

    assert res.status == 400
    assert payload["error"] == "rename_payload_must_be_object"
    await client.close()


@pytest.mark.asyncio
async def test_rename_title_must_be_string(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    await client.post("/api/chat", json={"message": "hello", "session_id": "s1"})
    res = await client.post("/api/sessions/s1/rename", json={"name": ["bad"]})
    payload = await res.json()

    assert res.status == 400
    assert payload["error"] == "title_required"
    await client.close()


@pytest.mark.asyncio
async def test_delete_from_here_and_edit_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    first = await (await client.post("/api/chat", json={"message": "hello", "session_id": "s1"})).json()
    second = await (await client.post("/api/chat", json={"message": "again", "session_id": "s1"})).json()
    second_user_id = second["user_message_id"]
    second_assistant_id = second["assistant_message_id"]
    deleted = await client.post(f"/api/sessions/s1/messages/{second_user_id}/delete-from-here", json={})
    deleted_body = await deleted.json()
    assert deleted.status == 200
    assert deleted_body["success"] is True
    assert deleted_body["mutation"] == "delete_from_here"
    assert deleted_body["metadata"]["strategy"] in {"fork_before_target", "new_empty_session"}
    session_after = await (await client.get("/api/sessions/s1")).json()
    assert session_after["session_id"] == "s1"
    ids = [m["id"] for m in session_after["messages"]]
    assert second_user_id not in ids
    assert second_assistant_id not in ids

    first_deleted = await client.post(f"/api/sessions/s1/messages/{first['user_message_id']}/delete-from-here", json={})
    assert first_deleted.status == 200
    first_deleted_payload = await first_deleted.json()
    assert first_deleted_payload["metadata"]["strategy"] == "new_empty_session"
    empty_session = await (await client.get("/api/sessions/s1")).json()
    assert empty_session["messages"] == []

    refill = await client.post("/api/chat", json={"message": "hello", "session_id": "s1"})
    refill_body = await refill.json()
    edit = await client.post(f"/api/sessions/s1/messages/{refill_body['user_message_id']}/edit", json={"content": "edited"})
    edit_body = await edit.json()
    assert edit.status == 200
    assert edit_body["replacement_user_message_id"]
    assert edit_body["response"] == "echo: edited"
    updated = await (await client.get("/api/sessions/s1")).json()
    contents = [m["content"] for m in updated["messages"]]
    assert "edited" in contents
    assert "hello" not in contents

    reject = await client.post(f"/api/sessions/s1/messages/{edit_body['assistant_message_id']}/edit", json={"content": "bad"})
    reject_body = await reject.json()
    assert reject.status == 400
    assert reject_body["error"] == "only_user_message_edit_supported"
    await client.close()


@pytest.mark.asyncio
async def test_edit_second_user_message_preserves_first_assistant(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    await client.post("/api/chat", json={"message": "hi", "session_id": "s1"})
    second = await (await client.post("/api/chat", json={"message": "how are you", "session_id": "s1"})).json()
    edit = await client.post(f"/api/sessions/s1/messages/{second['user_message_id']}/edit", json={"content": "how are u??"})
    edit_body = await edit.json()

    assert edit.status == 200
    assert edit_body["metadata"]["prefix_validated"] is True
    assert edit_body["metadata"]["expected_prefix_count"] == 2
    assert _role_content_pairs(edit_body["messages"]) == [
        ("user", "hi"),
        ("assistant", "echo: hi"),
        ("user", "how are u??"),
        ("assistant", "echo: how are u??"),
    ]

    session = await (await client.get("/api/sessions/s1")).json()
    contents = [message["content"] for message in session["messages"]]
    assert _role_content_pairs(session["messages"]) == _role_content_pairs(edit_body["messages"])
    assert "echo: hi" in contents
    assert "how are you" not in contents
    await client.close()


class _BlockingAsyncEditClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.edit_send_started = asyncio.Event()
        self.edit_send_release = asyncio.Event()
        self.send_message_calls: list[dict] = []

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        user_text = parts[0].get("text", "") if parts and isinstance(parts[0], dict) else ""
        self.send_message_calls.append({"session_id": session_id, "text": user_text, "message_id": message_id})
        if user_text == "how are u??":
            self.edit_send_started.set()
            await self.edit_send_release.wait()
        user = {"id": message_id or f"u-{len(self.messages[session_id])+1}", "role": "user", "parts": [{"type": "text", "text": user_text}]}
        assistant = {
            "id": f"a-{len(self.messages[session_id])+2}",
            "role": "assistant",
            "parts": [{"type": "text", "text": f"echo: {user_text}"}],
        }
        self.messages[session_id].extend([user, assistant])
        return {"message": assistant, "usage": {"input_tokens": 10, "output_tokens": 5, "cost": 0.001}, "model": model or "test-model", "provider": "test-provider"}


@pytest.mark.asyncio
async def test_async_edit_returns_before_llm_completion(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _BlockingAsyncEditClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()

    await client.post("/api/chat", json={"message": "hi", "session_id": "s1"})
    second = await (await client.post("/api/chat", json={"message": "how are you", "session_id": "s1"})).json()
    res = await asyncio.wait_for(
        client.post(f"/api/sessions/s1/messages/{second['user_message_id']}/edit/async", json={"content": "how are u??"}),
        timeout=0.5,
    )
    body = await res.json()

    assert res.status == 202
    assert body["success"] is True
    assert body["accepted"] is True
    assert body["async"] is True
    assert body["completion_state"] == "pending"
    assert body["request_id"]
    assert body["replacement_user_message_id"]
    assert body["assistant_message_id"] == ""
    assert body["response"] == ""
    assert body["metadata"]["prefix_validated"] is True
    assert body["metadata"]["edit_async"] is True
    assert body["metadata"]["background_started"] is True
    assert _role_content_pairs(body["messages"]) == [
        ("user", "hi"),
        ("assistant", "echo: hi"),
    ]
    assert ("assistant", "echo: how are u??") not in _role_content_pairs(body["messages"])

    tasks = list(app[TASK_BACKGROUND_TASKS_KEY])
    assert tasks
    await asyncio.wait_for(fake.edit_send_started.wait(), timeout=1)
    assert fake.send_message_calls[-1]["message_id"] == body["replacement_user_message_id"]
    fake.edit_send_release.set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)

    session = await (await client.get("/api/sessions/s1")).json()
    assert _role_content_pairs(session["messages"]) == [
        ("user", "hi"),
        ("assistant", "echo: hi"),
        ("user", "how are u??"),
        ("assistant", "echo: how are u??"),
    ]
    await client.close()


class _TrackingSendClient(FakeOpenCodeClient):
    def __init__(self, fork_mode: str = "include_boundary"):
        super().__init__(fork_mode=fork_mode)
        self.send_message_calls = 0

    async def send_message(self, *args, **kwargs):
        self.send_message_calls += 1
        return await super().send_message(*args, **kwargs)


class _FailingAsyncEditClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.send_message_calls: list[dict] = []

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        user_text = parts[0].get("text", "") if parts and isinstance(parts[0], dict) else ""
        self.send_message_calls.append({"session_id": session_id, "text": user_text, "message_id": message_id})
        if user_text == "how are u??":
            raise RuntimeError("simulated resend failure token=ghp_secretvalue")
        return await super().send_message(
            session_id,
            parts=parts,
            model=model,
            agent=agent,
            system=system,
            message_id=message_id,
            no_reply=no_reply,
            tools=tools,
        )


async def _drain_background_tasks(app):
    tasks = list(app[TASK_BACKGROUND_TASKS_KEY])
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)


async def _get_session_until(client, session_id, predicate):
    last_session = None
    for _ in range(20):
        last_session = await (await client.get(f"/api/sessions/{session_id}")).json()
        if predicate(last_session):
            return last_session
        await asyncio.sleep(0.05)
    raise AssertionError(f"session predicate never matched: {last_session}")


@pytest.mark.asyncio
async def test_async_edit_background_resend_failure_exposed_in_session_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _FailingAsyncEditClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()

    await client.post("/api/chat", json={"message": "hi", "session_id": "s1"})
    second = await (await client.post("/api/chat", json={"message": "how are you", "session_id": "s1"})).json()
    res = await client.post(
        f"/api/sessions/s1/messages/{second['user_message_id']}/edit/async",
        json={"content": "how are u??", "request_id": "req-edit-fail"},
    )
    body = await res.json()

    assert res.status == 202
    assert body["success"] is True
    assert body["accepted"] is True
    await _drain_background_tasks(app)

    session = await _get_session_until(
        client,
        "s1",
        lambda payload: payload.get("metadata", {}).get("latest_event_type") == "edit.failed",
    )
    metadata = session["metadata"]
    assert metadata["latest_event_type"] == "edit.failed"
    assert metadata["latest_event_state"] == "error"
    assert metadata["completion_state"] == "error"
    assert metadata["request_id"] == "req-edit-fail"
    assert metadata["chatlog_status"] == "failed"
    assert "simulated resend failure" in metadata["error"]
    assert "simulated resend failure" in metadata["incomplete_reason"]
    assert metadata["runtime_events"][0]["data"]["error"].startswith("simulated resend failure")
    assert "ghp_secretvalue" not in json.dumps(metadata)
    assert _role_content_pairs(session["messages"]) == [
        ("user", "hi"),
        ("assistant", "echo: hi"),
    ]
    assert ("assistant", "echo: how are u??") not in _role_content_pairs(session["messages"])
    await client.close()


@pytest.mark.asyncio
async def test_async_edit_rejects_assistant_message_without_background_task(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _TrackingSendClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()

    chat = await (await client.post("/api/chat", json={"message": "hi", "session_id": "s1"})).json()
    old_opencode_session_id = app[SESSION_STORE_KEY].get("s1").opencode_session_id
    old_messages = deepcopy(fake.messages[old_opencode_session_id])
    send_calls_before_edit = fake.send_message_calls

    res = await client.post(f"/api/sessions/s1/messages/{chat['assistant_message_id']}/edit/async", json={"content": "bad"})
    body = await res.json()

    assert res.status == 400
    assert body["error"] == "only_user_message_edit_supported"
    assert fake.send_message_calls == send_calls_before_edit
    assert app[TASK_BACKGROUND_TASKS_KEY] == set()
    assert app[SESSION_STORE_KEY].get("s1").opencode_session_id == old_opencode_session_id
    assert fake.messages[old_opencode_session_id] == old_messages

    session = await (await client.get("/api/sessions/s1")).json()
    assert _role_content_pairs(session["messages"]) == [
        ("user", "hi"),
        ("assistant", "echo: hi"),
    ]
    await client.close()


@pytest.mark.asyncio
async def test_async_edit_prefix_mismatch_does_not_start_background_task(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _TrackingSendClient(fork_mode="all_forks_bad_prefix")
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()

    await client.post("/api/chat", json={"message": "hi", "session_id": "s1"})
    second = await (await client.post("/api/chat", json={"message": "how are you", "session_id": "s1"})).json()
    old_opencode_session_id = app[SESSION_STORE_KEY].get("s1").opencode_session_id
    old_messages = deepcopy(fake.messages[old_opencode_session_id])
    send_calls_before_edit = fake.send_message_calls

    res = await client.post(f"/api/sessions/s1/messages/{second['user_message_id']}/edit/async", json={"content": "how are u??"})
    body = await res.json()

    assert res.status == 409
    assert body["error"] == "opencode_fork_prefix_mismatch"
    assert fake.send_message_calls == send_calls_before_edit
    assert app[TASK_BACKGROUND_TASKS_KEY] == set()
    assert app[SESSION_STORE_KEY].get("s1").opencode_session_id == old_opencode_session_id
    assert fake.messages[old_opencode_session_id] == old_messages

    session = await (await client.get("/api/sessions/s1")).json()
    assert _role_content_pairs(session["messages"]) == [
        ("user", "hi"),
        ("assistant", "echo: hi"),
        ("user", "how are you"),
        ("assistant", "echo: how are you"),
    ]
    await client.close()


@pytest.mark.asyncio
async def test_edit_skips_assistant_boundary_fork_that_drops_assistant(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = FakeOpenCodeClient(fork_mode="assistant_boundary_drops_assistant")
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()

    first = await (await client.post("/api/chat", json={"message": "hi", "session_id": "s1"})).json()
    second = await (await client.post("/api/chat", json={"message": "how are you", "session_id": "s1"})).json()
    edit = await client.post(f"/api/sessions/s1/messages/{second['user_message_id']}/edit", json={"content": "how are u??"})
    edit_body = await edit.json()

    assert edit.status == 200
    metadata = edit_body["metadata"]
    assistant_attempt = next(item for item in metadata["attempted_boundaries"] if item["role"] == "assistant")
    assert assistant_attempt["result"] == "prefix_mismatch"
    assert assistant_attempt["actual_prefix_count"] == 1
    assert metadata["accepted_boundary_message_id"] == first["user_message_id"]
    assert metadata["accepted_boundary_role"] == "user"
    assert metadata["prefix_validated"] is True
    assert _role_content_pairs(edit_body["messages"]) == [
        ("user", "hi"),
        ("assistant", "echo: hi"),
        ("user", "how are u??"),
        ("assistant", "echo: how are u??"),
    ]
    await client.close()


@pytest.mark.asyncio
async def test_delete_from_here_prefix_mismatch_keeps_original_mapping_and_history(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = FakeOpenCodeClient(fork_mode="all_forks_bad_prefix")
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()

    await client.post("/api/chat", json={"message": "hi", "session_id": "s1"})
    second = await (await client.post("/api/chat", json={"message": "how are you", "session_id": "s1"})).json()
    old_opencode_session_id = app[SESSION_STORE_KEY].get("s1").opencode_session_id
    res = await client.post(f"/api/sessions/s1/messages/{second['user_message_id']}/delete-from-here", json={})
    body = await res.json()

    assert res.status == 409
    assert body["error"] == "opencode_fork_prefix_mismatch"
    assert body["expected_prefix_count"] == 2
    assert body["actual_prefix_count"] == 1
    assert body["metadata"]["prefix_validated"] is False
    assert app[SESSION_STORE_KEY].get("s1").opencode_session_id == old_opencode_session_id

    session = await (await client.get("/api/sessions/s1")).json()
    assert _role_content_pairs(session["messages"]) == [
        ("user", "hi"),
        ("assistant", "echo: hi"),
        ("user", "how are you"),
        ("assistant", "echo: how are you"),
    ]
    await client.close()


@pytest.mark.asyncio
async def test_delete_from_here_allow_revert_fallback_does_not_mutate_old_session(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = FakeOpenCodeClient(fork_mode="all_forks_bad_prefix")
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()

    await client.post("/api/chat", json={"message": "hi", "session_id": "s1"})
    second = await (await client.post("/api/chat", json={"message": "how are you", "session_id": "s1"})).json()
    old_opencode_session_id = app[SESSION_STORE_KEY].get("s1").opencode_session_id
    old_messages = deepcopy(fake.messages[old_opencode_session_id])

    res = await client.post(
        f"/api/sessions/s1/messages/{second['user_message_id']}/delete-from-here",
        json={"allow_revert_fallback": True},
    )
    body = await res.json()

    assert res.status == 409
    assert body["error"] == "opencode_fork_prefix_mismatch"
    assert body["metadata"]["allow_revert_fallback_requested"] is True
    assert body["metadata"]["revert_fallback_disabled"] is True
    assert fake.revert_calls == []
    assert fake.messages[old_opencode_session_id] == old_messages
    assert app[SESSION_STORE_KEY].get("s1").opencode_session_id == old_opencode_session_id

    session = await (await client.get("/api/sessions/s1")).json()
    assert _role_content_pairs(session["messages"]) == [
        ("user", "hi"),
        ("assistant", "echo: hi"),
        ("user", "how are you"),
        ("assistant", "echo: how are you"),
    ]
    await client.close()


@pytest.mark.asyncio
async def test_edit_allow_revert_fallback_does_not_mutate_old_session(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = FakeOpenCodeClient(fork_mode="all_forks_bad_prefix")
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()

    await client.post("/api/chat", json={"message": "hi", "session_id": "s1"})
    second = await (await client.post("/api/chat", json={"message": "how are you", "session_id": "s1"})).json()
    old_opencode_session_id = app[SESSION_STORE_KEY].get("s1").opencode_session_id
    old_messages = deepcopy(fake.messages[old_opencode_session_id])

    res = await client.post(
        f"/api/sessions/s1/messages/{second['user_message_id']}/edit",
        json={"content": "how are u??", "allow_revert_fallback": True},
    )
    body = await res.json()

    assert res.status == 409
    assert body["error"] == "opencode_fork_prefix_mismatch"
    assert body["metadata"]["allow_revert_fallback_requested"] is True
    assert body["metadata"]["revert_fallback_disabled"] is True
    assert fake.revert_calls == []
    assert fake.messages[old_opencode_session_id] == old_messages
    assert app[SESSION_STORE_KEY].get("s1").opencode_session_id == old_opencode_session_id

    session = await (await client.get("/api/sessions/s1")).json()
    contents = [message["content"] for message in session["messages"]]
    assert _role_content_pairs(session["messages"]) == [
        ("user", "hi"),
        ("assistant", "echo: hi"),
        ("user", "how are you"),
        ("assistant", "echo: how are you"),
    ]
    assert "how are u??" not in contents
    assert "echo: how are u??" not in contents
    await client.close()


def test_extract_opencode_session_id_accepts_nested_shapes():
    assert _extract_opencode_session_id({"id": "ses-1"}) == "ses-1"
    assert _extract_opencode_session_id({"session": {"id": "ses-2"}}) == "ses-2"
    assert _extract_opencode_session_id({"data": {"sessionID": "ses-3"}}) == "ses-3"
    assert _extract_opencode_session_id({"message": {"id": "m-1"}}) == ""


@pytest.mark.asyncio
async def test_delete_from_here_missing_opencode_session_returns_opencode_session_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = FakeOpenCodeClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()
    chat = await (await client.post("/api/chat", json={"message": "hello", "session_id": "s1"})).json()
    record = app[SESSION_STORE_KEY].get("s1")
    missing_sid = record.opencode_session_id
    fake.sessions.pop(missing_sid, None)
    fake.messages.pop(missing_sid, None)
    original_list_messages = fake.list_messages

    async def _missing_404(session_id):
        if session_id == missing_sid:
            raise OpenCodeClientError("missing", status=404)
        return await original_list_messages(session_id)

    fake.list_messages = _missing_404
    res = await client.post(f"/api/sessions/s1/messages/{chat['user_message_id']}/delete-from-here", json={})
    body = await res.json()
    assert res.status == 404
    assert body["error"] == "opencode_session_not_found"
    await client.close()


class _List404Client(FakeOpenCodeClient):
    async def list_messages(self, session_id):
        raise OpenCodeClientError("missing", status=404)


@pytest.mark.asyncio
async def test_edit_list_messages_404_returns_json(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=_List404Client())
    client = TestClient(TestServer(app))
    await client.start_server()
    await client.post("/api/chat", json={"message": "hello", "session_id": "s1"})
    res = await client.post("/api/sessions/s1/messages/u-1/edit", json={"content": "x"})
    body = await res.json()
    assert res.status == 404
    assert body["error"] == "opencode_session_not_found"
    await client.close()


class _ResendFailClient(FakeOpenCodeClient):
    async def send_message(self, *args, **kwargs):
        parts = kwargs.get("parts") or []
        text = parts[0].get("text", "") if parts and isinstance(parts[0], dict) else ""
        if text == "edited":
            raise OpenCodeClientError("send failed", status=500)
        return await super().send_message(*args, **kwargs)


@pytest.mark.asyncio
async def test_edit_resend_failure_returns_502_json(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _ResendFailClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()
    chat = await (await client.post("/api/chat", json={"message": "hello", "session_id": "s1"})).json()
    res = await client.post(f"/api/sessions/s1/messages/{chat['user_message_id']}/edit", json={"content": "edited"})
    body = await res.json()
    assert res.status == 502
    assert body["error"] == "opencode_edit_resend_failed"
    assert "application/json" in res.headers.get("Content-Type", "")
    await client.close()


@pytest.mark.asyncio
async def test_delete_from_here_raw_upstream_exception_returns_502_json(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = FakeOpenCodeClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()
    chat = await (await client.post("/api/chat", json={"message": "hello", "session_id": "s1"})).json()
    record = app[SESSION_STORE_KEY].get("s1")
    target_sid = record.opencode_session_id
    original = fake.list_messages

    async def boom(session_id):
        if session_id == target_sid:
            raise RuntimeError("network down")
        return await original(session_id)

    fake.list_messages = boom
    res = await client.post(f"/api/sessions/s1/messages/{chat['user_message_id']}/delete-from-here", json={})
    body = await res.json()
    assert res.status == 502
    assert "application/json" in res.headers.get("Content-Type", "")
    assert body["error"] == "opencode_mutation_failed"
    assert "network down" in body["detail"]
    await client.close()


class _RawResendFailClient(FakeOpenCodeClient):
    async def send_message(self, *args, **kwargs):
        parts = kwargs.get("parts") or []
        text = parts[0].get("text", "") if parts and isinstance(parts[0], dict) else ""
        if text == "edited":
            raise RuntimeError("transport closed")
        return await super().send_message(*args, **kwargs)


@pytest.mark.asyncio
async def test_edit_raw_resend_exception_returns_502_json(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _RawResendFailClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()
    chat = await (await client.post("/api/chat", json={"message": "hello", "session_id": "s1"})).json()
    res = await client.post(f"/api/sessions/s1/messages/{chat['user_message_id']}/edit", json={"content": "edited"})
    body = await res.json()
    assert res.status == 502
    assert body["error"] == "opencode_edit_resend_failed"
    assert "transport closed" in body["detail"]
    await client.close()

class _DeleteStatusClient(FakeOpenCodeClient):
    def __init__(self, status):
        super().__init__(); self.status=status; self.delete_calls=0
    async def delete_session(self, session_id):
        self.delete_calls += 1
        if self.status is None:
            return await super().delete_session(session_id)
        raise OpenCodeClientError("delete failed", status=self.status)


@pytest.mark.asyncio
async def test_delete_session_500_returns_502_and_keeps_active(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=_DeleteStatusClient(500))
    client = TestClient(TestServer(app)); await client.start_server()
    await client.post('/api/chat', json={'message':'hello','session_id':'s1'})
    res = await client.delete('/api/sessions/s1'); body = await res.json()
    assert res.status == 502 and body['error'] == 'opencode_delete_failed'
    assert app[SESSION_STORE_KEY].get('s1').deleted is False
    await client.close()


@pytest.mark.asyncio
async def test_delete_session_404_marks_deleted(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=_DeleteStatusClient(404))
    client = TestClient(TestServer(app)); await client.start_server()
    await client.post('/api/chat', json={'message':'hello','session_id':'s1'})
    res = await client.delete('/api/sessions/s1'); body = await res.json()
    assert res.status == 200 and body['success'] is True and body['opencode_missing'] is True
    assert app[SESSION_STORE_KEY].get('s1').deleted is True
    await client.close()

@pytest.mark.asyncio
async def test_clear_sessions_partial_failure_returns_502(tmp_path, monkeypatch):
    class C(FakeOpenCodeClient):
        async def delete_session(self, session_id):
            if session_id.endswith('2'):
                raise OpenCodeClientError('boom', status=500)
            return await super().delete_session(session_id)
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    app = create_app(Settings.from_env(), opencode_client=C())
    client = TestClient(TestServer(app)); await client.start_server()
    await client.post('/api/chat', json={'message':'a','session_id':'s1'})
    await client.post('/api/chat', json={'message':'b','session_id':'s2'})
    res = await client.post('/api/clear'); body = await res.json()
    assert res.status == 502 and body['success'] is False and body['failed_count'] == 1
    assert app[SESSION_STORE_KEY].get('s1').deleted is True
    assert app[SESSION_STORE_KEY].get('s2').deleted is False
    await client.close()


@pytest.mark.asyncio
async def test_delete_session_metadata_exception_does_not_fail_delete(tmp_path, monkeypatch):
    class PM:
        async def publish_session_metadata(self, **kwargs): return {"success": True}
        async def delete_session_metadata(self, session_id): raise RuntimeError("x" * 5000)
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    app[PORTAL_METADATA_CLIENT_KEY] = PM()
    client = TestClient(TestServer(app)); await client.start_server()
    await client.post("/api/chat", json={"message":"hello","session_id":"s1"})
    res = await client.delete("/api/sessions/s1"); body = await res.json()
    assert res.status == 200 and body["success"] is True
    assert body["metadata_delete"]["success"] is False
    assert len(body["metadata_delete"]["error"]) <= 1010
    assert app[SESSION_STORE_KEY].get("s1").deleted is True
    await client.close()


@pytest.mark.asyncio
async def test_delete_session_already_deleted_skips_opencode_calls_metadata_best_effort(tmp_path, monkeypatch):
    class C(FakeOpenCodeClient):
        def __init__(self): super().__init__(); self.delete_calls=0
        async def delete_session(self, session_id): self.delete_calls += 1; return await super().delete_session(session_id)
    class PM:
        def __init__(self): self.calls=0
        async def publish_session_metadata(self, **kwargs): return {"success": True}
        async def delete_session_metadata(self, session_id): self.calls += 1; return {"success": True}
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    c=C(); pm=PM(); app=create_app(Settings.from_env(), opencode_client=c); app[PORTAL_METADATA_CLIENT_KEY]=pm
    client = TestClient(TestServer(app)); await client.start_server()
    await client.post("/api/chat", json={"message":"hello","session_id":"s1"})
    app[SESSION_STORE_KEY].mark_deleted("s1")
    res = await client.delete("/api/sessions/s1"); body = await res.json()
    assert res.status == 200 and body["already_deleted"] is True
    assert c.delete_calls == 0 and pm.calls == 1
    await client.close()


@pytest.mark.asyncio
async def test_delete_session_chatlog_delete_failure_reported_not_fatal(tmp_path, monkeypatch):
    class BadChatlog:
        def delete(self, _sid):
            raise RuntimeError("chatlog boom")

    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    app[CHATLOG_STORE_KEY] = BadChatlog()
    client = TestClient(TestServer(app)); await client.start_server()
    await client.post('/api/chat', json={'message':'hello','session_id':'s1'})
    res = await client.delete('/api/sessions/s1'); body = await res.json()
    assert res.status == 200 and body['success'] is True
    assert body['chatlog_delete']['success'] is False
    assert app[SESSION_STORE_KEY].get('s1').deleted is True
    await client.close()


@pytest.mark.asyncio
async def test_clear_sessions_chatlog_delete_failure_is_reported_not_fatal(tmp_path, monkeypatch):
    class BadChatlog:
        def delete(self, _sid):
            raise RuntimeError("chatlog boom")

    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    app[CHATLOG_STORE_KEY] = BadChatlog()
    client = TestClient(TestServer(app)); await client.start_server()
    await client.post('/api/chat', json={'message':'a','session_id':'s1'})
    res = await client.post('/api/clear'); body = await res.json()
    assert res.status == 200 and body['success'] is True
    assert body['metadata_delete'][0]['chatlog_delete']['success'] is False
    assert app[SESSION_STORE_KEY].get('s1').deleted is True
    await client.close()


@pytest.mark.asyncio
async def test_clear_sessions_partial_opencode_failure_skips_chatlog_and_metadata_for_failed_session(tmp_path, monkeypatch):
    class C(FakeOpenCodeClient):
        async def delete_session(self, session_id):
            if session_id.endswith('3'):
                raise OpenCodeClientError('boom', status=500)
            if session_id.endswith('2'):
                raise OpenCodeClientError('missing', status=404)
            return await super().delete_session(session_id)

    class TrackChatlog:
        def __init__(self): self.calls=[]
        def delete(self, sid): self.calls.append(sid); return True

    class PM:
        def __init__(self): self.calls=[]
        async def publish_session_metadata(self, **kwargs): return {"success": True}
        async def delete_session_metadata(self, sid): self.calls.append(sid); return {"success": True}

    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    c=C(); pm=PM(); chatlog=TrackChatlog()
    app = create_app(Settings.from_env(), opencode_client=c); app[PORTAL_METADATA_CLIENT_KEY]=pm; app[CHATLOG_STORE_KEY]=chatlog
    client = TestClient(TestServer(app)); await client.start_server()
    await client.post('/api/chat', json={'message':'a','session_id':'s1'})
    await client.post('/api/chat', json={'message':'b','session_id':'s2'})
    await client.post('/api/chat', json={'message':'c','session_id':'s3'})
    res = await client.post('/api/clear'); body = await res.json()
    assert res.status == 502 and body['success'] is False and body['deleted_count'] == 2 and body['failed_count'] == 1
    assert app[SESSION_STORE_KEY].get('s1').deleted is True
    assert app[SESSION_STORE_KEY].get('s2').deleted is True
    assert app[SESSION_STORE_KEY].get('s3').deleted is False
    assert set(pm.calls) == {'s1','s2'}
    assert set(chatlog.calls) == {'s1','s2'}
    await client.close()


def test_to_efp_messages_filters_internal_auto_continue_metadata():
    raw = [
        {"id": "msg_user_1", "role": "user", "parts": [{"type": "text", "text": "hi"}]},
        {"id": "msg_internal_1", "role": "user", "parts": [{"type": "text", "text": "continue", "metadata": {"efp_internal": "auto_continue"}}]},
        {"id": "efp-auto-continue-legacy", "role": "user", "parts": [{"type": "text", "text": "legacy"}]},
    ]
    out = _to_efp_messages(raw)
    ids = [m.get("id") for m in out]
    assert "msg_user_1" in ids
    assert "msg_internal_1" not in ids
    assert "efp-auto-continue-legacy" not in ids


def test_to_efp_messages_uses_display_sidecar_for_user_content_and_attachments(tmp_path):
    class DisplayStore:
        def get_user_message(self, opencode_session_id, opencode_message_id, portal_session_id=None):
            assert opencode_session_id == "ses_1"
            assert opencode_message_id == "msg_1"
            assert portal_session_id == "portal_1"
            return {
                "display_content": "/jira-bulk-create-from-csv example: https://jira.company.com/browse/MMGFX-13887",
                "display_attachments": [
                    {
                        "file_id": "file_1",
                        "name": "cases.csv",
                        "content_type": "text/csv",
                        "size": 123,
                        "type": "file",
                        "parsed": True,
                    }
                ],
            }

    raw = [
        {
            "info": {"id": "msg_1", "role": "user"},
            "parts": [
                {
                    "type": "text",
                    "text": "Run the OpenCode agent skill `jira-bulk-create-from-csv`.\n\nOriginal user slash command:\n`/jira-bulk-create-from-csv example: https://jira.company.com/browse/MMGFX-13887`\n\nAttached files:\n## cases.csv\nsummary,steps\nA,B",
                }
            ],
        }
    ]

    out = _to_efp_messages(
        raw,
        display_store=DisplayStore(),
        portal_session_id="portal_1",
        opencode_session_id="ses_1",
    )

    assert out[0]["content"] == "/jira-bulk-create-from-csv example: https://jira.company.com/browse/MMGFX-13887"
    assert out[0]["display_content"] == out[0]["content"]
    assert out[0]["attachments"] == [
        {"file_id": "file_1", "name": "cases.csv", "content_type": "text/csv", "size": 123, "type": "file", "parsed": True}
    ]
    assert out[0]["metadata"]["display_content_source"] == "portal_original_user_message"
    assert out[0]["metadata"]["internal_model_content_hidden"] is True
    assert "Run the OpenCode agent skill" not in out[0]["content"]
    assert "Attached files:" not in out[0]["content"]
    assert "summary,steps" not in out[0]["content"]
