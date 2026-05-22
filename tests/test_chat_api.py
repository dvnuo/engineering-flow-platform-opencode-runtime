import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.app_keys import SESSION_STORE_KEY
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


class EmptyFinalClient(FakeOpenCodeClient):
    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        self.messages[session_id].append({"id": message_id or "msg_user_empty", "role": "user", "parts": parts})
        return {"message": {"id": message_id or "msg_user_empty", "role": "user", "parts": parts}}


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
        assert payload["response"] == "OpenCode completed without a visible assistant response."
    finally:
        await client.close()
