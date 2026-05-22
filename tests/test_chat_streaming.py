import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


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
