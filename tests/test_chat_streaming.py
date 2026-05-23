import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


class DelayedAssistantClient(FakeOpenCodeClient):
    def __init__(self, final_text: str, *, visible_after_lists: int = 2):
        super().__init__()
        self.final_text = final_text
        self.visible_after_lists = visible_after_lists
        self._after_send_lists: dict[str, int] = {}
        self._assistant_added: set[str] = set()

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        user = {"id": message_id or "u-delayed-stream", "role": "user", "parts": parts}
        self.messages[session_id].append(user)
        self._after_send_lists[session_id] = 0
        return {"message": user}

    async def list_messages(self, session_id):
        if session_id in self._after_send_lists and session_id not in self._assistant_added:
            self._after_send_lists[session_id] += 1
            if self._after_send_lists[session_id] >= self.visible_after_lists:
                self.messages[session_id].append(
                    {
                        "id": "a-delayed-stream",
                        "role": "assistant",
                        "parts": [{"type": "text", "text": self.final_text}],
                    }
                )
                self._assistant_added.add(session_id)
        return list(self.messages.get(session_id, []))


def _sse_events(body):
    events = []
    for chunk in body.strip().split("\n\n"):
        event_name = ""
        data = ""
        for line in chunk.splitlines():
            if line.startswith("event: "):
                event_name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data = line.removeprefix("data: ")
        if event_name and data:
            events.append((event_name, json.loads(data)))
    return events


@pytest.mark.asyncio
async def test_chat_stream_is_simple_sse_wrapper(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())))
    await client.start_server()
    try:
        resp = await client.post("/api/chat/stream", json={"message": "hello stream", "session_id": "s-stream", "request_id": "r-stream"})
        body = await resp.text()
        events = _sse_events(body)
        names = [name for name, _payload in events]

        assert resp.status == 200
        assert names == ["chat.started", "final", "done"]
        final_payload = dict(events)["final"]
        assert final_payload["completion_state"] == "completed"
        assert final_payload["response"] == "echo: hello stream"
        assert "heartbeat" not in names
        assert "chat.stream_detached" not in body
        assert "continuation." not in body
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_chat_stream_waits_for_delayed_assistant_visible_response(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_POLL_SECONDS", "0.1")
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=DelayedAssistantClient("delayed stream final"))))
    await client.start_server()
    try:
        resp = await client.post("/api/chat/stream", json={"message": "hello stream", "session_id": "s-stream-delayed", "request_id": "r-stream-delayed"})
        body = await resp.text()
        events = _sse_events(body)
        names = [name for name, _payload in events]

        assert resp.status == 200
        assert names == ["chat.started", "final", "done"]
        final_payload = dict(events)["final"]
        assert final_payload["completion_state"] == "completed"
        assert final_payload["response"] == "delayed stream final"
    finally:
        await client.close()
