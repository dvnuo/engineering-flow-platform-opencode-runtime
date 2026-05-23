import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.app_keys import SESSION_STORE_KEY
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


class EmptyFinalClient(FakeOpenCodeClient):
    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        user = {"id": message_id or "msg_user_empty", "role": "user", "parts": parts}
        assistant = {"id": "a-empty", "role": "assistant", "finish_reason": "stop", "parts": []}
        self.messages[session_id].extend([user, assistant])
        return {"message": assistant}


class DelayedAssistantClient(FakeOpenCodeClient):
    def __init__(self, final_text: str, *, visible_after_lists: int = 2):
        super().__init__()
        self.final_text = final_text
        self.visible_after_lists = visible_after_lists
        self._after_send_lists: dict[str, int] = {}
        self._assistant_added: set[str] = set()

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        user = {"id": message_id or "u-delayed", "role": "user", "parts": parts}
        self.messages[session_id].append(user)
        self._after_send_lists[session_id] = 0
        return {"message": user}

    async def list_messages(self, session_id):
        if session_id in self._after_send_lists and session_id not in self._assistant_added:
            self._after_send_lists[session_id] += 1
            if self._after_send_lists[session_id] >= self.visible_after_lists:
                self.messages[session_id].append(
                    {
                        "id": "a-delayed",
                        "role": "assistant",
                        "parts": [{"type": "text", "text": self.final_text}],
                    }
                )
                self._assistant_added.add(session_id)
        return list(self.messages.get(session_id, []))


class NoAssistantClient(FakeOpenCodeClient):
    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        user = {"id": message_id or "u-timeout", "role": "user", "parts": parts}
        self.messages[session_id].append(user)
        return {"message": user}


def _event_types(payload):
    return [event.get("type") for event in payload.get("runtime_events", [])]


@pytest.mark.asyncio
async def test_chat_short_request_completes_without_long_task_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/api/chat", json={"message": "hello", "session_id": "s1", "request_id": "r1"})
        payload = await resp.json()

        assert resp.status == 200
        assert payload["ok"] is True
        assert payload["completion_state"] == "completed"
        assert payload["response"] == "echo: hello"
        assert payload["session_id"] == "s1"
        assert app[SESSION_STORE_KEY].get("s1").last_message == "echo: hello"
        assert {"chat.started", "llm_thinking", "chat.completed"} <= set(_event_types(payload))
        encoded = json.dumps(payload)
        for forbidden in ("continuation.", "timeout_recovery", "transport_recovery", "stream_detached", "chat_run"):
            assert forbidden not in encoded
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_chat_waits_for_delayed_assistant_visible_response(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_POLL_SECONDS", "0.1")
    app = create_app(Settings.from_env(), opencode_client=DelayedAssistantClient("delayed final"))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/api/chat", json={"message": "wait", "session_id": "s-delayed", "request_id": "r-delayed"})
        payload = await resp.json()

        assert resp.status == 200
        assert payload["ok"] is True
        assert payload["completion_state"] == "completed"
        assert payload["response"] == "delayed final"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_chat_timeout_without_assistant_is_incomplete(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_POLL_SECONDS", "0.1")
    app = create_app(Settings.from_env(), opencode_client=NoAssistantClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/api/chat", json={"message": "no assistant", "session_id": "s-timeout", "request_id": "r-timeout"})
        payload = await resp.json()

        assert resp.status == 200
        assert payload["ok"] is False
        assert payload["completion_state"] == "incomplete"
        assert payload["incomplete_reason"] == "final_assistant_message_timeout"
        assert payload["response"] != "OpenCode completed without a visible assistant response."
        failed_events = [event for event in payload["runtime_events"] if event["type"] in {"execution.failed", "chat.failed"}]
        assert failed_events
        assert all(event["data"]["completion_state"] == "incomplete" for event in failed_events)
        assert all(event["data"]["incomplete_reason"] == "final_assistant_message_timeout" for event in failed_events)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_chat_empty_final_is_not_success(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=EmptyFinalClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/api/chat", json={"message": "no final", "session_id": "s-empty"})
        payload = await resp.json()

        assert resp.status == 200
        assert payload["ok"] is False
        assert payload["completion_state"] == "empty_final"
        assert payload["incomplete_reason"] == "empty_final_assistant_text"
        assert payload["response"] == "OpenCode completed without a visible assistant response."
        failed_events = [event for event in payload["runtime_events"] if event["type"] in {"execution.failed", "chat.failed"}]
        assert failed_events
        assert all(event["data"]["completion_state"] == "empty_final" for event in failed_events)
        assert all(event["data"]["incomplete_reason"] == "empty_final_assistant_text" for event in failed_events)
    finally:
        await client.close()
