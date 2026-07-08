"""Regressions for long-running chat stream resilience in the adapter.

Covers: SSE keepalive comments during idle streaming, startup sweep of
interrupted running chatlog entries, and /api/chat request_id idempotency.
"""

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter import chat_api
from efp_opencode_adapter.chat_api import (
    _stream_runtime_events_until_done,
    chat_handler,
    chat_run_registry,
)
from efp_opencode_adapter.chatlog_store import ChatLogStore
from efp_opencode_adapter.opencode_client import OpenCodeTransportTimeout
from efp_opencode_adapter.recovery import RecoveryManager
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.session_store import SessionStore
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.state import ensure_state_dirs
from test_t06_helpers import FakeOpenCodeClient


class _RecordingResponse:
    def __init__(self):
        self.writes = []

    async def write(self, data):
        self.writes.append(data.decode())


class _EmptySubscriber:
    def __init__(self):
        self.queue = asyncio.Queue()


@pytest.mark.asyncio
async def test_stream_loop_writes_keepalive_comments_while_idle(monkeypatch):
    # The interval floor is 1s; simulate an idle window slightly above it.
    monkeypatch.setenv("EFP_CHAT_SSE_KEEPALIVE_SECONDS", "1")
    resp = _RecordingResponse()
    chat_task = asyncio.create_task(asyncio.sleep(1.4))

    await _stream_runtime_events_until_done(resp, _EmptySubscriber(), chat_task, set())

    keepalive_chunks = [chunk for chunk in resp.writes if chunk.startswith(": keepalive")]
    assert keepalive_chunks, "expected SSE keepalive comments during idle streaming"
    assert all(chunk.endswith("\n\n") for chunk in keepalive_chunks)
    assert all("data:" not in chunk for chunk in keepalive_chunks)


class _EndlessDuplicateSubscriber:
    """Steady stream of one repeated event id (all dedup after the first).

    Events arrive every 50ms so the loop keeps taking the successful-get
    branch (never the idle timeout branch) while writing no bytes, and the
    event loop still schedules the chat task's timer normally.
    """

    def __init__(self):
        self.queue = self

    async def get(self):
        await asyncio.sleep(0.05)
        return {"id": "dup-1", "type": "runtime_event", "request_id": "r-dup"}

    def get_nowait(self):
        # The post-loop drain sees an empty queue; the endless supply above
        # only models the live-streaming phase.
        raise asyncio.QueueEmpty


@pytest.mark.asyncio
async def test_stream_loop_keeps_alive_when_only_duplicate_events_arrive(monkeypatch):
    # Deduplicated events write no bytes; a stream of duplicates must not
    # suppress keepalives or intermediaries still hit idle read timeouts.
    monkeypatch.setenv("EFP_CHAT_SSE_KEEPALIVE_SECONDS", "1")
    resp = _RecordingResponse()
    chat_task = asyncio.create_task(asyncio.sleep(1.4))

    await _stream_runtime_events_until_done(resp, _EndlessDuplicateSubscriber(), chat_task, set())

    event_chunks = [chunk for chunk in resp.writes if chunk.startswith("event: runtime_event")]
    keepalive_chunks = [chunk for chunk in resp.writes if chunk.startswith(": keepalive")]
    assert len(event_chunks) == 1
    assert keepalive_chunks, "expected keepalives while only duplicate events arrive"


@pytest.mark.asyncio
async def test_stream_loop_does_not_write_keepalive_when_stream_is_short(monkeypatch):
    monkeypatch.setenv("EFP_CHAT_SSE_KEEPALIVE_SECONDS", "60")
    resp = _RecordingResponse()
    chat_task = asyncio.create_task(asyncio.sleep(0.2))

    await _stream_runtime_events_until_done(resp, _EmptySubscriber(), chat_task, set())

    assert not any(chunk.startswith(": keepalive") for chunk in resp.writes)


class _JsonRequest:
    def __init__(self, payload):
        self._payload = payload
        self.app = {}

    async def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_chat_handler_conflicts_on_active_duplicate_request_id():
    request_id = "chat-dedupe-active-1"
    session_id = "s-dedupe-1"
    chat_run_registry._records.pop(request_id, None)
    chat_run_registry.start(session_id=session_id, request_id=request_id)
    try:
        response = await chat_handler(_JsonRequest({"message": "again", "session_id": session_id, "request_id": request_id}))
    finally:
        chat_run_registry._records.pop(request_id, None)

    assert isinstance(response, web.Response)
    assert response.status == 409
    assert b"duplicate_chat_request_id" in response.body


@pytest.mark.asyncio
async def test_chat_handler_replays_final_payload_for_completed_duplicate_request_id():
    request_id = "chat-dedupe-final-1"
    session_id = "s-dedupe-2"
    chat_run_registry._records.pop(request_id, None)
    chat_run_registry.start(session_id=session_id, request_id=request_id)
    chat_run_registry.complete(
        request_id,
        {
            "ok": True,
            "completion_state": "completed",
            "response": "already answered",
            "session_id": session_id,
            "request_id": request_id,
        },
    )
    try:
        response = await chat_handler(_JsonRequest({"message": "again", "session_id": session_id, "request_id": request_id}))
    finally:
        chat_run_registry._records.pop(request_id, None)

    assert response.status == 200
    assert b"already answered" in response.body


@pytest.mark.asyncio
async def test_recovery_marks_running_chatlog_entries_interrupted(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    settings = Settings.from_env()
    paths = ensure_state_dirs(settings)
    chatlog_store = ChatLogStore(paths.chatlogs_dir)
    chatlog_store.start_entry("s-restart", request_id="r-running", message="long task")
    chatlog_store.start_entry("s-restart", request_id="r-finished", message="short task")
    chatlog_store.finish_entry("s-restart", request_id="r-finished", status="success", response="done")

    manager = RecoveryManager(
        settings=settings,
        state_paths=paths,
        session_store=SessionStore(paths.sessions_dir),
        chatlog_store=chatlog_store,
        opencode_client=FakeOpenCodeClient(),
    )
    summary = await manager.recover()

    assert summary["chat_entries_marked_interrupted"] == 1
    entries = {entry["request_id"]: entry for entry in chatlog_store.get("s-restart")["entries"]}
    assert entries["r-running"]["status"] == "error"
    assert "restart" in entries["r-running"]["response"].lower()
    assert entries["r-finished"]["status"] == "success"
    assert entries["r-finished"]["response"] == "done"

    # Idempotent: a second recovery pass finds nothing else to mark.
    summary_again = await manager.recover()
    assert summary_again["chat_entries_marked_interrupted"] == 0


def test_timeout_text_explains_background_continuation():
    text = chat_api._non_success_assistant_text("incomplete", "final_assistant_message_timeout")
    assert "background" in text
    assert "history" in text


def test_transport_timeout_is_marked_recoverable():
    # The blocking session message POST lasts as long as the run itself, so a
    # submit timeout must route into the send acceptance probe instead of
    # failing a chat whose message OpenCode is still executing.
    exc = OpenCodeTransportTimeout("POST", "/session/oc-1/message", 900)
    assert exc.is_transport_timeout is True
    assert exc.is_recoverable_transport_error is True


class AcceptedThenTimedOutClient(FakeOpenCodeClient):
    """Send blocks past the submit timeout while the run keeps executing."""

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        user = {"id": message_id or "u-timeout", "role": "user", "parts": parts}
        assistant = {
            "id": "a-timeout",
            "role": "assistant",
            "parts": [{"type": "text", "text": "long run final"}],
        }
        self.messages[session_id].extend([user, assistant])
        raise OpenCodeTransportTimeout("POST", f"/session/{session_id}/message", 1)


@pytest.mark.asyncio
async def test_chat_recovers_when_send_times_out_while_run_continues(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_POLL_SECONDS", "0.1")
    app = create_app(Settings.from_env(), opencode_client=AcceptedThenTimedOutClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post(
            "/api/chat",
            json={"message": "long task", "session_id": "s-timeout", "request_id": "r-timeout"},
        )
        payload = await resp.json()

        assert resp.status == 200
        assert payload["ok"] is True
        assert payload["completion_state"] == "completed"
        assert payload["response"] == "long run final"
        event_types = [event.get("type") for event in payload.get("runtime_events", [])]
        assert "chat.failed" not in event_types
        assert "execution.failed" not in event_types
        assert payload["_llm_debug"]["send_disconnect_probe"]["accepted"] is True
    finally:
        await client.close()
