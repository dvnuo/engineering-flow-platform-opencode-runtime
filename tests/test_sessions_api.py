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

    ch = await client.get(f"/api/sessions/{sid}/chatlog")
    body = await ch.json()
    assert body["success"] is True
    assert "chatlog" in body
    assert "runtime_events" in body
    assert "events" in body

    rn = await client.post(f"/api/sessions/{sid}/rename", json={"name": "renamed"})
    assert (await rn.json())["success"] is True
    ls2 = await client.get("/api/sessions")
    assert (await ls2.json())["sessions"][0]["name"] == "renamed"

    missing = await client.post(f"/api/sessions/{sid}/messages/missing/delete-from-here", json={})
    assert missing.status == 404

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


@pytest.mark.asyncio
async def test_rename_invalid_json_returns_400_json(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    await client.post("/api/chat", json={"message": "hello", "session_id": "s1"})
    res = await client.post("/api/sessions/s1/rename", data='{"name":', headers={"Content-Type": "application/json"})
    payload = await res.json()

    assert res.status == 400
    assert payload["error"] == "invalid_json"
    await client.close()


@pytest.mark.asyncio
async def test_rename_payload_must_be_object(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    await client.post("/api/chat", json={"message": "hello", "session_id": "s1"})
    res = await client.post("/api/sessions/s1/rename", json=["bad"])
    payload = await res.json()

    assert res.status == 400
    assert payload["error"] == "rename_payload_must_be_object"
    await client.close()


@pytest.mark.asyncio
async def test_rename_title_must_be_string(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    await client.post("/api/chat", json={"message": "hello", "session_id": "s1"})
    res = await client.post("/api/sessions/s1/rename", json={"name": ["bad"]})
    payload = await res.json()

    assert res.status == 400
    assert payload["error"] == "title_required"
    await client.close()


@pytest.mark.asyncio
async def test_delete_from_here_and_edit_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    first = await (await client.post("/api/chat", json={"message": "hello", "session_id": "s1"})).json()
    second = await (await client.post("/api/chat", json={"message": "again", "session_id": "s1"})).json()
    second_user_id = second["user_message_id"]
    second_assistant_id = second["assistant_message_id"]
    deleted = await client.post(f"/api/sessions/s1/messages/{second_user_id}/delete-from-here", json={})
    deleted_body = await deleted.json()
    assert deleted.status == 200
    assert deleted_body["success"] is True
    assert deleted_body["mutation"] == "delete_from_here"
    assert deleted_body["metadata"]["strategy"] in {"fork_before_target", "new_empty_session"}
    session_after = await (await client.get("/api/sessions/s1")).json()
    assert session_after["session_id"] == "s1"
    ids = [m["id"] for m in session_after["messages"]]
    assert second_user_id not in ids
    assert second_assistant_id not in ids

    first_deleted = await client.post(f"/api/sessions/s1/messages/{first['user_message_id']}/delete-from-here", json={})
    assert first_deleted.status == 200
    first_deleted_payload = await first_deleted.json()
    assert first_deleted_payload["metadata"]["strategy"] == "new_empty_session"
    empty_session = await (await client.get("/api/sessions/s1")).json()
    assert empty_session["messages"] == []

    refill = await client.post("/api/chat", json={"message": "hello", "session_id": "s1"})
    refill_body = await refill.json()
    edit = await client.post(f"/api/sessions/s1/messages/{refill_body['user_message_id']}/edit", json={"content": "edited"})
    edit_body = await edit.json()
    assert edit.status == 200
    assert edit_body["replacement_user_message_id"]
    assert edit_body["response"] == "echo: edited"
    updated = await (await client.get("/api/sessions/s1")).json()
    contents = [m["content"] for m in updated["messages"]]
    assert "edited" in contents
    assert "hello" not in contents

    reject = await client.post(f"/api/sessions/s1/messages/{edit_body['assistant_message_id']}/edit", json={"content": "bad"})
    reject_body = await reject.json()
    assert reject.status == 400
    assert reject_body["error"] == "only_user_message_edit_supported"
    await client.close()
