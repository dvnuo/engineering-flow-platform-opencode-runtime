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


async def _iter(items):
    for item in items:
        yield item


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
    assert out[1][1]["message_id"] == "msg-1"
    assert out[1][1]["messageId"] == "msg-1"
    assert out[1][1]["raw"]["messageID"] == "msg-1"
    assert out[2][1]["partID"] == "part-1"
    assert out[2][1]["part_id"] == "part-1"
    assert out[2][1]["partId"] == "part-1"
    assert out[2][1]["data"]["partID"] == "part-1"


@pytest.mark.asyncio
async def test_event_proxy_session_idle_sets_idle_status():
    out = [
        item
        async for item in iter_filtered_events(
            _iter([{"type": "session.idle", "sessionID": "ses-1"}]),
            conversation_id="pc-1",
            opencode_session_id="ses-1",
        )
    ]

    assert out[0][0] == "opencode.session.status"
    payload = out[0][1]
    assert payload["status"] == "idle"
    assert payload["active"] is False
    assert payload["can_abort"] is False
    assert payload["can_send"] is True
    assert payload["action_hint"] == "safe_to_send"
    assert payload["snapshot_required"] is True
    assert payload["data"] == {}
    assert payload["raw"]["type"] == "session.idle"


@pytest.mark.asyncio
async def test_event_proxy_status_event_sets_send_abort_hints():
    out = [
        item
        async for item in iter_filtered_events(
            _iter(
                [
                    {"type": "session.updated", "sessionID": "ses-1", "data": {"state": "busy"}},
                    {"type": "session.updated", "sessionID": "ses-1", "data": {"state": "idle"}},
                    {"type": "session.updated", "sessionID": "ses-1", "data": {"state": "unknown"}},
                ]
            ),
            conversation_id="pc-1",
            opencode_session_id="ses-1",
        )
    ]

    assert out[0][1]["status"] == "busy"
    assert out[0][1]["active"] is True
    assert out[0][1]["can_abort"] is True
    assert out[0][1]["can_send"] is False
    assert out[0][1]["action_hint"] == "wait_or_stop"
    assert out[1][1]["status"] == "idle"
    assert out[1][1]["can_send"] is True
    assert out[1][1]["action_hint"] == "safe_to_send"
    assert out[2][1]["status"] == "unknown"
    assert out[2][1]["can_send"] is False
    assert out[2][1]["action_hint"] == "refresh_status"


@pytest.mark.asyncio
async def test_event_proxy_preserves_id_aliases_and_objects():
    out = [
        item
        async for item in iter_filtered_events(
            _iter(
                [
                    {
                        "type": "message.updated",
                        "sessionID": "ses-1",
                        "messageID": "msg-1",
                        "data": {"message": {"id": "msg-1", "role": "assistant"}},
                    },
                    {
                        "type": "message.part.updated",
                        "sessionID": "ses-1",
                        "data": {"partID": "part-1", "part": {"id": "part-1", "type": "text"}},
                    },
                    {
                        "type": "permission.requested",
                        "sessionID": "ses-1",
                        "permissionID": "perm-1",
                        "data": {"permission": {"id": "perm-1", "tool": "bash"}},
                    },
                ]
            ),
            conversation_id="pc-1",
            opencode_session_id="ses-1",
        )
    ]

    assert out[0][1]["messageID"] == "msg-1"
    assert out[0][1]["message_id"] == "msg-1"
    assert out[0][1]["messageId"] == "msg-1"
    assert out[0][1]["message"]["id"] == "msg-1"
    assert out[1][1]["partID"] == "part-1"
    assert out[1][1]["part_id"] == "part-1"
    assert out[1][1]["partId"] == "part-1"
    assert out[1][1]["part"]["id"] == "part-1"
    assert out[2][1]["permissionID"] == "perm-1"
    assert out[2][1]["permission_id"] == "perm-1"
    assert out[2][1]["permissionId"] == "perm-1"
    assert out[2][1]["permission"]["id"] == "perm-1"


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
