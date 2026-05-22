import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


class ThinOpenCodeClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.states: dict[str, str] = {}
        self.children_by_session: dict[str, list[dict]] = {}
        self.mcp_payload = {"github": {"status": "connected"}}

    async def create_session(self, title=None, parent_id=None):
        session = await super().create_session(title=title, parent_id=parent_id)
        self.states[session["id"]] = "idle"
        return session

    async def get_session_status(self):
        return {"sessions": {sid: {"state": self.states.get(sid, "idle")} for sid in self.sessions}}

    async def list_session_children(self, session_id):
        return self.children_by_session.get(session_id, [])

    async def mcp_status(self):
        return {"servers": self.mcp_payload}


async def _client(tmp_path, monkeypatch, fake=None):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PORTAL_AGENT_ID", "agent-thin")
    app = create_app(Settings.from_env(), opencode_client=fake or ThinOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, app


async def _create_conversation(client: TestClient, title="New chat"):
    resp = await client.post("/api/opencode/conversations", json={"title": title})
    assert resp.status == 200
    body = await resp.json()
    return body["conversation"]


@pytest.mark.asyncio
async def test_create_list_get_and_update_conversation(tmp_path, monkeypatch):
    fake = ThinOpenCodeClient()
    client, app = await _client(tmp_path, monkeypatch, fake)
    try:
        conversation = await _create_conversation(client)

        assert conversation["id"].startswith("pc_")
        assert conversation["agent_id"] == "agent-thin"
        assert conversation["opencode_session_id"] == "ses-1"
        assert conversation["source"] == "opencode"
        assert fake.sessions["ses-1"]["title"] == "New chat"

        listing = await (await client.get("/api/opencode/conversations")).json()
        assert listing["ok"] is True
        assert listing["conversations"][0]["id"] == conversation["id"]
        assert listing["conversations"][0]["status"]["type"] == "idle"

        detail = await (await client.get(f"/api/opencode/conversations/{conversation['id']}")).json()
        assert detail["conversation"]["opencode_session_id"] == "ses-1"
        assert "4096" not in json.dumps(detail)

        renamed = await client.patch(f"/api/opencode/conversations/{conversation['id']}", json={"title": "Renamed"})
        assert renamed.status == 200
        assert (await renamed.json())["conversation"]["title"] == "Renamed"
        assert fake.sessions["ses-1"]["title"] == "Renamed"

        deleted = await client.delete(f"/api/opencode/conversations/{conversation['id']}")
        assert deleted.status == 200
        assert (await client.get(f"/api/opencode/conversations/{conversation['id']}")).status == 404

        include_archived = await (await client.get("/api/opencode/conversations?include_archived=true")).json()
        assert include_archived["conversations"][0]["archived_at"]
        assert "4096" not in json.dumps(include_archived)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_status_uses_opencode_status_without_long_task_store(tmp_path, monkeypatch):
    fake = ThinOpenCodeClient()
    client, app = await _client(tmp_path, monkeypatch, fake)
    try:
        conversation = await _create_conversation(client)
        fake.states["ses-1"] = "running"

        busy = await client.get(f"/api/opencode/conversations/{conversation['id']}/status")
        body = await busy.json()
        assert busy.status == 200
        assert body["status"]["type"] == "busy"
        assert body["status"]["active"] is True
        assert body["status"]["can_send"] is False
        assert body["status"]["can_abort"] is True

        fake.states["ses-1"] = "idle"
        idle = await (await client.get(f"/api/opencode/conversations/{conversation['id']}/status")).json()
        assert idle["status"]["type"] == "idle"
        assert idle["status"]["active"] is False
        assert idle["status"]["can_send"] is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_child_busy_does_not_block_root_status(tmp_path, monkeypatch):
    fake = ThinOpenCodeClient()
    client, _app = await _client(tmp_path, monkeypatch, fake)
    try:
        conversation = await _create_conversation(client)
        fake.sessions["child-1"] = {"id": "child-1", "title": "child"}
        fake.states["ses-1"] = "idle"
        fake.states["child-1"] = "busy"
        fake.children_by_session["ses-1"] = [{"id": "child-1"}]

        body = await (await client.get(f"/api/opencode/conversations/{conversation['id']}/status")).json()

        assert body["status"]["type"] == "idle"
        assert body["status"]["active"] is False
        assert body["status"]["can_send"] is True
        assert body["children"]["active_count"] == 1
        assert body["children"]["non_blocking"] is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_messages_return_canonical_opencode_snapshot(tmp_path, monkeypatch):
    fake = ThinOpenCodeClient()
    client, _app = await _client(tmp_path, monkeypatch, fake)
    try:
        conversation = await _create_conversation(client)
        fake.messages["ses-1"] = [
            {"info": {"id": "msg-user", "role": "user"}, "parts": [{"type": "text", "text": "hi"}]},
            {"info": {"id": "msg-assistant", "role": "assistant"}, "parts": [{"type": "text", "text": "hello"}]},
        ]

        body = await (await client.get(f"/api/opencode/conversations/{conversation['id']}/messages?limit=200")).json()

        assert body["ok"] is True
        assert body["source_of_truth"] == "opencode"
        assert body["messages"] == fake.messages["ses-1"]
        assert "canonical_messages" not in body
        assert "assistant_projection" not in json.dumps(body)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mcp_status_is_servers_map_not_tools_list(tmp_path, monkeypatch):
    fake = ThinOpenCodeClient()
    client, _app = await _client(tmp_path, monkeypatch, fake)
    try:
        body = await (await client.get("/api/opencode/mcp")).json()
        assert body["ok"] is True
        assert body["servers"] == {"github": {"status": "connected"}}
        assert "tools" not in body
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_thin_api_does_not_expose_internal_opencode_url(tmp_path, monkeypatch):
    fake = ThinOpenCodeClient()
    client, _app = await _client(tmp_path, monkeypatch, fake)
    try:
        conversation = await _create_conversation(client)
        health = await (await client.get("/api/opencode/health")).text()
        status = await (await client.get(f"/api/opencode/conversations/{conversation['id']}/status")).text()
        assert "127.0.0.1:4096" not in health
        assert "127.0.0.1:4096" not in status
    finally:
        await client.close()


def test_thin_api_has_no_task_integration():
    source = Path("efp_opencode_adapter/opencode_thin_api.py").read_text(encoding="utf-8")
    assert "/api/tasks" not in source
    assert "TASK_STORE" not in source
