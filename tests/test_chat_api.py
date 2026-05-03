import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


@pytest.mark.asyncio
async def test_chat_and_stream(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = FakeOpenCodeClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()

    r1 = await client.post("/api/chat", json={"message": "hello"})
    assert r1.status == 200
    p1 = await r1.json()
    assert p1["session_id"]
    assert p1["request_id"]
    assert p1["response"] == "echo: hello"
    assert p1["_llm_debug"]["engine"] == "opencode"
    assert p1["_llm_debug"]["opencode_session_id"]

    index = tmp_path / "state" / "sessions" / "index.json"
    assert index.exists()

    sid = p1["session_id"]
    op_sid = p1["_llm_debug"]["opencode_session_id"]
    r2 = await client.post("/api/chat", json={"message": "again", "session_id": sid})
    p2 = await r2.json()
    assert p2["_llm_debug"]["opencode_session_id"] == op_sid
    assert fake.create_calls == 1

    r3 = await client.post("/api/chat", json={"message": "x", "session_id": "portal-1"})
    assert (await r3.json())["session_id"] == "portal-1"

    r4 = await client.post("/api/chat", json={"message": ""})
    assert r4.status == 400

    rs = await client.post("/api/chat/stream", json={"message": "hello stream"})
    body = await rs.text()
    assert rs.status == 200
    assert "text/event-stream" in rs.headers.get("Content-Type", "")
    assert "event: final" in body
    assert "event: done" in body
    await client.close()
