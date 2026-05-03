import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.opencode_client import OpenCodeClientError
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


class FailingGetClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.fail_get = False

    async def get_session(self, session_id):
        if self.fail_get:
            raise OpenCodeClientError("upstream unavailable", status=503)
        return await super().get_session(session_id)


class FailingCreateClient(FakeOpenCodeClient):
    async def create_session(self, title=None):
        self.create_calls += 1
        raise OpenCodeClientError("create failed", status=503)


class MissingIdCreateClient(FakeOpenCodeClient):
    async def create_session(self, title=None):
        self.create_calls += 1
        return {"title": title or "Chat"}


class FailingListMessagesClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.fail_list = False

    async def list_messages(self, session_id):
        if self.fail_list:
            raise OpenCodeClientError("list failed", status=503)
        return await super().list_messages(session_id)


@pytest.mark.asyncio
async def test_existing_session_get_session_error_returns_502_and_failed_event(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = FailingGetClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()

    first = await client.post("/api/chat", json={"message": "hello", "session_id": "portal-1"})
    assert first.status == 200
    assert fake.create_calls == 1

    ws = await client.ws_connect("/api/events?session_id=portal-1")
    assert (await ws.receive_json())["type"] == "connected"

    fake.fail_get = True
    second = await client.post("/api/chat", json={"message": "again", "session_id": "portal-1"})
    body = await second.json()

    assert second.status == 502
    assert body["error"] == "opencode_error"
    assert fake.create_calls == 1

    evt = await ws.receive_json(timeout=2)
    assert evt["type"] == "execution.failed"
    assert evt["session_id"] == "portal-1"
    assert evt["request_id"]
    assert evt["opencode_session_id"]

    await ws.close()
    await client.close()


@pytest.mark.asyncio
async def test_create_session_error_returns_502_and_failed_event(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = FailingCreateClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()

    ws = await client.ws_connect("/api/events?session_id=portal-new")
    assert (await ws.receive_json())["type"] == "connected"

    res = await client.post("/api/chat", json={"message": "hello", "session_id": "portal-new"})
    payload = await res.json()

    assert res.status == 502
    assert payload["error"] == "opencode_error"

    evt = await ws.receive_json(timeout=2)
    assert evt["type"] == "execution.failed"
    assert evt["session_id"] == "portal-new"

    await ws.close()
    await client.close()


@pytest.mark.asyncio
async def test_create_session_missing_id_returns_502_json(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=MissingIdCreateClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    res = await client.post("/api/chat", json={"message": "hello", "session_id": "portal-new"})
    payload = await res.json()
    assert res.status == 502
    assert payload["error"] == "opencode_error"
    assert "no session id" in payload["detail"]

    await client.close()


@pytest.mark.asyncio
async def test_chat_stream_upstream_error_emits_sse_error(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FailingCreateClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    res = await client.post("/api/chat/stream", json={"message": "hello", "session_id": "portal-stream"})
    body = await res.text()

    assert res.status == 200
    assert "text/event-stream" in res.headers.get("Content-Type", "")
    assert "event: runtime_event" in body
    assert "event: error" in body
    assert "chat_failed" in body or "opencode_error" in body

    await client.close()


@pytest.mark.asyncio
async def test_session_detail_list_messages_error_returns_502_json(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = FailingListMessagesClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()

    created = await client.post("/api/chat", json={"message": "hello", "session_id": "portal-1"})
    sid = (await created.json())["session_id"]

    fake.fail_list = True
    res = await client.get(f"/api/sessions/{sid}")
    payload = await res.json()

    assert res.status == 502
    assert payload["error"] == "opencode_error"

    await client.close()
