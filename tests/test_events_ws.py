import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


@pytest.mark.asyncio
async def test_events_ws(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    ws = await client.ws_connect("/api/events")
    first = await ws.receive_json()
    assert first == {"type": "connected", "engine": "opencode"}

    await client.post("/api/chat", json={"message": "hello", "session_id": "portal-1"})
    events = []
    for _ in range(6):
        events.append(await ws.receive_json(timeout=2))
        if events[-1].get("type") == "execution.completed":
            break
    types = {e["type"] for e in events}
    assert "execution.started" in types
    assert "llm_thinking" in types
    assert "execution.completed" in types

    ws2 = await client.ws_connect("/api/events?session_id=portal-filter")
    assert (await ws2.receive_json())["type"] == "connected"
    await client.post("/api/chat", json={"message": "x", "session_id": "portal-other"})
    await client.post("/api/chat", json={"message": "y", "session_id": "portal-filter"})

    got = await ws2.receive_json(timeout=2)
    assert got["session_id"] == "portal-filter"
    await ws.close()
    await ws2.close()
    await client.close()
