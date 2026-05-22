import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


class ThinSendClient(FakeOpenCodeClient):
    def __init__(self, state="idle"):
        super().__init__()
        self.state = state
        self.send_calls = []
        self.abort_calls = []

    async def get_session_status(self):
        return {"sessions": {sid: {"state": self.state} for sid in self.sessions}}

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        self.send_calls.append({"session_id": session_id, "parts": parts, "model": model, "agent": agent, "message_id": message_id})
        return await super().send_message(session_id, parts=parts, model=model, agent=agent, system=system, message_id=message_id, no_reply=no_reply, tools=tools)

    async def prompt_async(self, session_id, payload):
        raise AssertionError("thin send must not call prompt_async")

    async def abort_session(self, session_id):
        self.abort_calls.append(session_id)
        self.state = "idle"
        return {"success": True, "supported": True, "status": 200}


async def _setup(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()
    created = await client.post("/api/opencode/conversations", json={"title": "Chat"})
    conversation = (await created.json())["conversation"]
    return client, conversation


@pytest.mark.asyncio
async def test_thin_send_is_synchronous_and_returns_messages(tmp_path, monkeypatch):
    fake = ThinSendClient(state="idle")
    client, conversation = await _setup(tmp_path, monkeypatch, fake)
    try:
        resp = await client.post(
            "/api/opencode/conversations/%s/send" % conversation["id"],
            json={"text": "hello", "message_id": "msg_user_123", "model": "anthropic/claude-sonnet", "agent": "build"},
        )
        payload = await resp.json()

        assert resp.status == 200
        assert payload["status"] == "completed"
        assert payload["action_hint"] == "refresh_messages"
        assert payload["messages"]
        assert fake.send_calls[-1]["message_id"] == "msg_user_123"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_thin_send_busy_and_abort_are_simple_status_paths(tmp_path, monkeypatch):
    fake = ThinSendClient(state="busy")
    client, conversation = await _setup(tmp_path, monkeypatch, fake)
    try:
        busy = await client.post("/api/opencode/conversations/%s/send" % conversation["id"], json={"text": "hello"})
        busy_payload = await busy.json()
        assert busy.status == 409
        assert busy_payload["error"] == "opencode_session_busy"
        assert "chat_run_already_active" not in str(busy_payload)

        aborted = await client.post("/api/opencode/conversations/%s/abort" % conversation["id"])
        aborted_payload = await aborted.json()
        assert aborted.status == 200
        assert aborted_payload["status"]["type"] == "idle"
        assert fake.abort_calls == ["ses-1"]
    finally:
        await client.close()
