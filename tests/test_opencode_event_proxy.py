import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.opencode_event_proxy import iter_filtered_events
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


async def _events():
    yield {"type": "server.connected", "data": {"hello": True}}
    yield {"type": "message.updated", "sessionID": "other", "messageID": "msg-other"}
    yield {"type": "message.updated", "sessionID": "ses-1", "messageID": "msg-1"}
    yield {"type": "message.part.updated", "data": {"sessionID": "ses-1", "partID": "part-1"}}
    yield {"type": "message.part.delta", "data": {"sessionID": "other", "partID": "part-other"}}


@pytest.mark.asyncio
async def test_event_proxy_filters_session_and_normalizes_events():
    out = [
        item
        async for item in iter_filtered_events(
            _events(),
            conversation_id="pc-1",
            opencode_session_id="ses-1",
        )
    ]

    assert [name for name, _payload in out] == [
        "opencode.connected",
        "opencode.message.updated",
        "opencode.message.part.updated",
    ]
    assert out[1][1]["conversation_id"] == "pc-1"
    assert out[1][1]["opencode_session_id"] == "ses-1"
    assert out[1][1]["messageID"] == "msg-1"
    assert out[2][1]["partID"] == "part-1"


class EventStreamClient(FakeOpenCodeClient):
    async def get_session_status(self):
        return {"sessions": {sid: {"state": "idle"} for sid in self.sessions}}

    async def event_stream(self):
        async for event in _events():
            yield event


@pytest.mark.asyncio
async def test_events_route_streams_filtered_sse(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = EventStreamClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        created = await client.post("/api/opencode/conversations", json={"title": "Chat"})
        conversation = (await created.json())["conversation"]

        resp = await client.get(f"/api/opencode/conversations/{conversation['id']}/events")
        body = await resp.text()

        assert resp.status == 200
        assert "text/event-stream" in resp.headers.get("Content-Type", "")
        assert "event: opencode.connected" in body
        assert "event: opencode.message.updated" in body
        assert "event: opencode.message.part.updated" in body
        assert "msg-other" not in body
        assert "part-other" not in body
    finally:
        await client.close()
