import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


@pytest.mark.asyncio
async def test_sessions_endpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    cr = await client.post("/api/chat", json={"message": "hello"})
    cp = await cr.json()
    sid = cp["session_id"]

    ls = await client.get("/api/sessions")
    lsp = await ls.json()
    assert len(lsp["sessions"]) == 1
    assert lsp["sessions"][0]["engine"] == "opencode"
    assert lsp["sessions"][0]["message_count"] >= 2

    dt = await client.get(f"/api/sessions/{sid}")
    dp = await dt.json()
    roles = [m["role"] for m in dp["messages"]]
    assert "user" in roles and "assistant" in roles
    assert dp["metadata"]["engine"] == "opencode"

    rn = await client.post(f"/api/sessions/{sid}/rename", json={"name": "renamed"})
    assert (await rn.json())["success"] is True
    ls2 = await client.get("/api/sessions")
    assert (await ls2.json())["sessions"][0]["name"] == "renamed"

    u1 = await client.post(f"/api/sessions/{sid}/messages/m1/edit", json={})
    u2 = await client.post(f"/api/sessions/{sid}/messages/m1/delete-from-here", json={})
    assert u1.status == 501 and u2.status == 501

    dl = await client.delete(f"/api/sessions/{sid}")
    assert (await dl.json())["success"] is True
    assert (await (await client.get("/api/sessions")).json())["sessions"] == []
    assert (await client.get(f"/api/sessions/{sid}")).status == 404

    await client.post("/api/chat", json={"message": "a", "session_id": "s1"})
    await client.post("/api/chat", json={"message": "b", "session_id": "s2"})
    cl = await client.post("/api/clear")
    assert (await cl.json())["success"] is True
    assert (await (await client.get("/api/sessions")).json())["sessions"] == []
    await client.close()
