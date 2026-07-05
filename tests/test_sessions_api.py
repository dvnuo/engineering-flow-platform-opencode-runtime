import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.app_keys import CHATLOG_STORE_KEY, SESSION_STORE_KEY
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.session_store import SessionRecord
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.thinking_events import utc_now_iso
from test_t06_helpers import FakeOpenCodeClient


@pytest.mark.asyncio
async def test_sessions_endpoints_keep_basic_opencode_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        chat = await client.post("/api/chat", json={"message": "hello", "session_id": "s1"})
        chat_payload = await chat.json()
        assert chat_payload["completion_state"] == "completed"

        listing = await (await client.get("/api/sessions")).json()
        assert listing["sessions"][0]["session_id"] == "s1"

        detail = await (await client.get("/api/sessions/s1")).json()
        assert detail["success"] is True
        assert detail["messages"]
        assert detail["canonical_messages"]
        assert detail["metadata"]["engine"] == "opencode"
        assert "active_run" not in json.dumps(detail["metadata"])

        renamed = await (await client.post("/api/sessions/s1/rename", json={"name": "Renamed"})).json()
        assert renamed["success"] is True

        deleted = await (await client.delete("/api/sessions/s1")).json()
        assert deleted["success"] is True
        assert "chat_runs_deleted" not in deleted
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_session_detail_exposes_running_chatlog_for_portal_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    now = utc_now_iso()
    app[SESSION_STORE_KEY].upsert(
        SessionRecord("s-running", "oc-running", "Running", None, None, now, now, "", 0)
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        # Create the running entry after startup: entries that exist before
        # startup are restart leftovers and are swept to error by recovery.
        app[CHATLOG_STORE_KEY].start_entry(
            "s-running",
            request_id="req-running",
            message="hello",
            runtime_events=[{"type": "llm_thinking", "summary": "Thinking"}],
        )
        detail = await (await client.get("/api/sessions/s-running")).json()

        assert detail["success"] is True
        metadata = detail["metadata"]
        assert metadata["chatlog_status"] == "running"
        assert metadata["latest_event_state"] == "running"
        assert metadata["completion_state"] == "running"
        assert metadata["request_id"] == "req-running"
        assert metadata["last_execution_id"] == "req-running"
        assert metadata["latest_request_id"] == "req-running"
        assert metadata["runtime_events"][-1]["type"] == "llm_thinking"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_sessions_listing_hides_task_sessions_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    now = utc_now_iso()
    app[SESSION_STORE_KEY].upsert(
        SessionRecord("s-human", "oc-human", "Human", None, None, now, "2026-06-21T00:03:00+00:00", "hello", 2)
    )
    app[SESSION_STORE_KEY].upsert(
        SessionRecord("agent-task:task-1", "oc-task-1", "Task", None, None, now, "2026-06-21T00:04:00+00:00", "task", 1)
    )
    app[SESSION_STORE_KEY].upsert(
        SessionRecord("agent-task-task-2", "oc-task-2", "Native Task", None, None, now, "2026-06-21T00:05:00+00:00", "task", 1)
    )
    app[SESSION_STORE_KEY].upsert(
        SessionRecord("task-task-3", "oc-task-3", "Fallback Task", None, None, now, "2026-06-21T00:06:00+00:00", "task", 1)
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        listing = await (await client.get("/api/sessions")).json()

        assert [item["session_id"] for item in listing["sessions"]] == ["s-human"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_sessions_listing_can_include_task_sessions_for_debug(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    now = utc_now_iso()
    app[SESSION_STORE_KEY].upsert(
        SessionRecord("s-human", "oc-human", "Human", None, None, now, "2026-06-21T00:03:00+00:00", "hello", 2)
    )
    app[SESSION_STORE_KEY].upsert(
        SessionRecord("agent-task:task-1", "oc-task-1", "Task", None, None, now, "2026-06-21T00:04:00+00:00", "task", 1)
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        listing = await (await client.get("/api/sessions?include_task_sessions=1")).json()

        assert [item["session_id"] for item in listing["sessions"]] == ["agent-task:task-1", "s-human"]
    finally:
        await client.close()
