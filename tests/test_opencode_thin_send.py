import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.app_keys import CHAT_RUN_STORE_KEY
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


class SendClient(FakeOpenCodeClient):
    def __init__(self, *, state="idle", abort_sets_idle=True):
        super().__init__()
        self.state = state
        self.abort_sets_idle = abort_sets_idle
        self.abort_calls: list[str] = []
        self.prompt_async_calls: list[dict] = []

    async def create_session(self, title=None, parent_id=None):
        session = await super().create_session(title=title, parent_id=parent_id)
        return session

    async def get_session_status(self):
        return {"sessions": {sid: {"state": self.state} for sid in self.sessions}}

    async def abort_session(self, session_id):
        self.abort_calls.append(session_id)
        if self.abort_sets_idle:
            self.state = "idle"
        return {"success": True, "supported": True, "status": 204}

    async def prompt_async(self, session_id, payload):
        self.prompt_async_calls.append({"session_id": session_id, "payload": payload})
        return None


async def _setup(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()
    created = await client.post("/api/opencode/conversations", json={"title": "Chat"})
    conversation = (await created.json())["conversation"]
    return client, app, conversation


@pytest.mark.asyncio
async def test_send_idle_calls_prompt_async_with_normalized_body(tmp_path, monkeypatch):
    fake = SendClient(state="idle")
    client, app, conversation = await _setup(tmp_path, monkeypatch, fake)
    try:
        def forbidden(*_args, **_kwargs):
            raise AssertionError("ChatRunStore must not be used by thin send")

        app[CHAT_RUN_STORE_KEY].active_for_session = forbidden
        app[CHAT_RUN_STORE_KEY].latest_for_session = forbidden

        resp = await client.post(
            f"/api/opencode/conversations/{conversation['id']}/send",
            json={
                "text": "hello",
                "message_id": "msg_user_123",
                "model": "anthropic/claude-sonnet",
                "agent": "build",
                "attachments": [],
            },
        )
        body = await resp.json()

        assert resp.status == 200
        assert body["status"] == "accepted"
        assert body["action_hint"] == "watch_events_then_reconcile"
        assert fake.prompt_async_calls == [
            {
                "session_id": "ses-1",
                "payload": {
                    "messageID": "msg_user_123",
                    "model": {"providerID": "anthropic", "modelID": "claude-sonnet"},
                    "agent": "build",
                    "parts": [{"type": "text", "text": "hello"}],
                },
            }
        ]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_send_passes_system_tools_and_no_reply_to_prompt_async(tmp_path, monkeypatch):
    fake = SendClient(state="idle")
    client, _app, conversation = await _setup(tmp_path, monkeypatch, fake)
    try:
        resp = await client.post(
            f"/api/opencode/conversations/{conversation['id']}/send",
            json={
                "text": "hello",
                "message_id": "msg_user_123",
                "system": "Use the repo instructions.",
                "tools": {"bash": False, "read": True},
                "noReply": True,
            },
        )
        body = await resp.json()

        assert resp.status == 200
        assert body["status"] == "accepted"
        assert fake.prompt_async_calls[-1]["payload"] == {
            "messageID": "msg_user_123",
            "parts": [{"type": "text", "text": "hello"}],
            "system": "Use the repo instructions.",
            "tools": {"bash": False, "read": True},
            "noReply": True,
        }
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_send_no_reply_alias_passes_no_reply_to_prompt_async(tmp_path, monkeypatch):
    fake = SendClient(state="idle")
    client, _app, conversation = await _setup(tmp_path, monkeypatch, fake)
    try:
        resp = await client.post(
            f"/api/opencode/conversations/{conversation['id']}/send",
            json={"text": "hello", "no_reply": True},
        )

        assert resp.status == 200
        assert fake.prompt_async_calls[-1]["payload"]["noReply"] is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_send_with_attachments_returns_400_without_prompt_async(tmp_path, monkeypatch):
    fake = SendClient(state="idle")
    client, _app, conversation = await _setup(tmp_path, monkeypatch, fake)
    try:
        resp = await client.post(
            f"/api/opencode/conversations/{conversation['id']}/send",
            json={
                "text": "hello",
                "attachments": [{"name": "context.txt", "content": "data"}],
            },
        )
        body = await resp.json()

        assert resp.status == 400
        assert body["error"] == "attachments_unsupported_for_thin_send"
        assert body["action_hint"] == "send_without_attachments_or_use_file_context"
        assert fake.prompt_async_calls == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_send_busy_returns_opencode_session_busy_without_prompt_async(tmp_path, monkeypatch):
    fake = SendClient(state="busy")
    client, _app, conversation = await _setup(tmp_path, monkeypatch, fake)
    try:
        resp = await client.post(
            f"/api/opencode/conversations/{conversation['id']}/send",
            json={"text": "hello", "message_id": "msg_user_123"},
        )
        body = await resp.json()

        assert resp.status == 409
        assert body["error"] == "opencode_session_busy"
        assert body["status"]["type"] == "busy"
        assert body["status"]["active"] is True
        assert body["status"]["can_send"] is False
        assert body["status"]["can_abort"] is True
        assert body["action_hint"] == "wait_or_stop"
        assert "chat_run_already_active" not in str(body)
        assert fake.prompt_async_calls == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_abort_returns_idle_after_opencode_abort(tmp_path, monkeypatch):
    fake = SendClient(state="busy", abort_sets_idle=True)
    client, _app, conversation = await _setup(tmp_path, monkeypatch, fake)
    try:
        resp = await client.post(f"/api/opencode/conversations/{conversation['id']}/abort")
        body = await resp.json()

        assert resp.status == 200
        assert fake.abort_calls == ["ses-1"]
        assert body["status"]["type"] == "idle"
        assert body["status"]["active"] is False
        assert body["status"]["can_send"] is True
        assert body["status"]["can_abort"] is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_abort_still_active_returns_409_without_hard_reset(tmp_path, monkeypatch):
    fake = SendClient(state="busy", abort_sets_idle=False)
    client, _app, conversation = await _setup(tmp_path, monkeypatch, fake)
    try:
        resp = await client.post(f"/api/opencode/conversations/{conversation['id']}/abort")
        body = await resp.json()

        assert resp.status == 409
        assert body["error"] == "opencode_abort_still_active"
        assert body["status"]["type"] == "busy"
        assert body["actions"] == ["retry_abort", "new_conversation"]
        assert fake.abort_calls == ["ses-1"]
        assert fake.create_calls == 1
    finally:
        await client.close()
