import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.app_keys import EVENT_BUS_KEY
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


@pytest.mark.asyncio
async def test_events_ws_replay_and_type_filter(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    await app[EVENT_BUS_KEY].publish({"type": "tool.started", "session_id": "portal-replay", "request_id": "req-replay", "data": {"tool": "bash"}})
    await app[EVENT_BUS_KEY].publish({"type": "provider.retry", "session_id": "portal-replay", "request_id": "req-replay", "data": {"attempt": 1}})
    await app[EVENT_BUS_KEY].publish({"type": "tool.completed", "session_id": "portal-replay", "request_id": "req-replay", "data": {"tool": "bash"}})

    ws = await client.ws_connect("/api/events?session_id=portal-replay&replay=1&types=tool.started,tool.completed")
    assert (await ws.receive_json())["type"] == "connected"
    first = await ws.receive_json(timeout=2)
    second = await ws.receive_json(timeout=2)

    assert [first["type"], second["type"]] == ["tool.started", "tool.completed"]
    assert first["metadata"]["replayed"] is True
    assert second["metadata"]["replayed"] is True

    await app[EVENT_BUS_KEY].publish({"type": "provider.retry", "session_id": "portal-replay", "request_id": "req-replay"})
    await app[EVENT_BUS_KEY].publish({"type": "tool.completed", "session_id": "portal-replay", "request_id": "req-replay", "data": {"tool": "bash"}})
    live = await ws.receive_json(timeout=2)
    assert live["type"] == "tool.completed"

    await ws.close()
    await client.close()


@pytest.mark.asyncio
async def test_events_ws_replay_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    await app[EVENT_BUS_KEY].publish({"type": "one", "request_id": "req-limit"})
    await app[EVENT_BUS_KEY].publish({"type": "two", "request_id": "req-limit"})
    await app[EVENT_BUS_KEY].publish({"type": "three", "request_id": "req-limit"})

    ws = await client.ws_connect("/api/events?request_id=req-limit&replay=1&replay_limit=2")
    assert (await ws.receive_json())["type"] == "connected"
    first = await ws.receive_json(timeout=2)
    second = await ws.receive_json(timeout=2)

    assert [first["type"], second["type"]] == ["two", "three"]

    await ws.close()
    await client.close()


@pytest.mark.asyncio
async def test_events_ws_replay_after_last_event_at(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    await app[EVENT_BUS_KEY].publish({"type": "one", "session_id": "portal-last", "created_at": "2026-05-18T00:00:00+00:00"})
    await app[EVENT_BUS_KEY].publish({"type": "two", "session_id": "portal-last", "created_at": "2026-05-18T00:00:01+00:00"})
    await app[EVENT_BUS_KEY].publish({"type": "three", "session_id": "portal-last", "created_at": "2026-05-18T00:00:02+00:00"})

    ws = await client.ws_connect("/api/events?session_id=portal-last&replay=1&last_event_at=2026-05-18T00:00:01Z")
    assert (await ws.receive_json())["type"] == "connected"
    event = await ws.receive_json(timeout=2)

    assert event["type"] == "three"

    await ws.close()
    await client.close()
