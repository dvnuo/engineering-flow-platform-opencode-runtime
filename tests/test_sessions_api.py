import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


@pytest.mark.asyncio
async def test_sessions_endpoints_keep_basic_opencode_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        chat = await client.post("/api/chat", json={"message": "hello", "session_id": "s1"})
        chat_payload = await chat.json()
        assert chat_payload["completion_state"] == "completed"

        listing = await (await client.get("/api/sessions")).json()
        assert listing["sessions"][0]["session_id"] == "s1"

        detail = await (await client.get("/api/sessions/s1")).json()
        assert detail["success"] is True
        assert detail["messages"]
        assert detail["canonical_messages"]
        assert detail["metadata"]["engine"] == "opencode"
        assert "active_run" not in json.dumps(detail["metadata"])

        renamed = await (await client.post("/api/sessions/s1/rename", json={"name": "Renamed"})).json()
        assert renamed["success"] is True

        deleted = await (await client.delete("/api/sessions/s1")).json()
        assert deleted["success"] is True
        assert "chat_runs_deleted" not in deleted
    finally:
        await client.close()
