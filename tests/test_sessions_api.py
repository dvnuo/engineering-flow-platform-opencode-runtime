import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.sessions_api import _extract_opencode_session_id, _to_efp_messages
from efp_opencode_adapter.opencode_client import OpenCodeClientError
from efp_opencode_adapter.app_keys import SESSION_STORE_KEY, PORTAL_METADATA_CLIENT_KEY, CHATLOG_STORE_KEY
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


def test_extract_opencode_session_id_accepts_nested_shapes():
    assert _extract_opencode_session_id({"id": "ses-1"}) == "ses-1"
    assert _extract_opencode_session_id({"session": {"id": "ses-2"}}) == "ses-2"
    assert _extract_opencode_session_id({"data": {"sessionID": "ses-3"}}) == "ses-3"
    assert _extract_opencode_session_id({"message": {"id": "m-1"}}) == ""


@pytest.mark.asyncio
async def test_delete_from_here_missing_opencode_session_returns_opencode_session_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = FakeOpenCodeClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()
    chat = await (await client.post("/api/chat", json={"message": "hello", "session_id": "s1"})).json()
    record = app[SESSION_STORE_KEY].get("s1")
    missing_sid = record.opencode_session_id
    fake.sessions.pop(missing_sid, None)
    fake.messages.pop(missing_sid, None)
    original_list_messages = fake.list_messages

    async def _missing_404(session_id):
        if session_id == missing_sid:
            raise OpenCodeClientError("missing", status=404)
        return await original_list_messages(session_id)

    fake.list_messages = _missing_404
    res = await client.post(f"/api/sessions/s1/messages/{chat['user_message_id']}/delete-from-here", json={})
    body = await res.json()
    assert res.status == 404
    assert body["error"] == "opencode_session_not_found"
    await client.close()


class _List404Client(FakeOpenCodeClient):
    async def list_messages(self, session_id):
        raise OpenCodeClientError("missing", status=404)


@pytest.mark.asyncio
async def test_edit_list_messages_404_returns_json(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=_List404Client())
    client = TestClient(TestServer(app))
    await client.start_server()
    await client.post("/api/chat", json={"message": "hello", "session_id": "s1"})
    res = await client.post("/api/sessions/s1/messages/u-1/edit", json={"content": "x"})
    body = await res.json()
    assert res.status == 404
    assert body["error"] == "opencode_session_not_found"
    await client.close()


class _ResendFailClient(FakeOpenCodeClient):
    async def send_message(self, *args, **kwargs):
        parts = kwargs.get("parts") or []
        text = parts[0].get("text", "") if parts and isinstance(parts[0], dict) else ""
        if text == "edited":
            raise OpenCodeClientError("send failed", status=500)
        return await super().send_message(*args, **kwargs)


@pytest.mark.asyncio
async def test_edit_resend_failure_returns_502_json(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _ResendFailClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()
    chat = await (await client.post("/api/chat", json={"message": "hello", "session_id": "s1"})).json()
    res = await client.post(f"/api/sessions/s1/messages/{chat['user_message_id']}/edit", json={"content": "edited"})
    body = await res.json()
    assert res.status == 502
    assert body["error"] == "opencode_edit_resend_failed"
    assert "application/json" in res.headers.get("Content-Type", "")
    await client.close()


@pytest.mark.asyncio
async def test_delete_from_here_raw_upstream_exception_returns_502_json(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = FakeOpenCodeClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()
    chat = await (await client.post("/api/chat", json={"message": "hello", "session_id": "s1"})).json()
    record = app[SESSION_STORE_KEY].get("s1")
    target_sid = record.opencode_session_id
    original = fake.list_messages

    async def boom(session_id):
        if session_id == target_sid:
            raise RuntimeError("network down")
        return await original(session_id)

    fake.list_messages = boom
    res = await client.post(f"/api/sessions/s1/messages/{chat['user_message_id']}/delete-from-here", json={})
    body = await res.json()
    assert res.status == 502
    assert "application/json" in res.headers.get("Content-Type", "")
    assert body["error"] == "opencode_mutation_failed"
    assert "network down" in body["detail"]
    await client.close()


class _RawResendFailClient(FakeOpenCodeClient):
    async def send_message(self, *args, **kwargs):
        parts = kwargs.get("parts") or []
        text = parts[0].get("text", "") if parts and isinstance(parts[0], dict) else ""
        if text == "edited":
            raise RuntimeError("transport closed")
        return await super().send_message(*args, **kwargs)


@pytest.mark.asyncio
async def test_edit_raw_resend_exception_returns_502_json(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _RawResendFailClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()
    chat = await (await client.post("/api/chat", json={"message": "hello", "session_id": "s1"})).json()
    res = await client.post(f"/api/sessions/s1/messages/{chat['user_message_id']}/edit", json={"content": "edited"})
    body = await res.json()
    assert res.status == 502
    assert body["error"] == "opencode_edit_resend_failed"
    assert "transport closed" in body["detail"]
    await client.close()

class _DeleteStatusClient(FakeOpenCodeClient):
    def __init__(self, status):
        super().__init__(); self.status=status; self.delete_calls=0
    async def delete_session(self, session_id):
        self.delete_calls += 1
        if self.status is None:
            return await super().delete_session(session_id)
        raise OpenCodeClientError("delete failed", status=self.status)


@pytest.mark.asyncio
async def test_delete_session_500_returns_502_and_keeps_active(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=_DeleteStatusClient(500))
    client = TestClient(TestServer(app)); await client.start_server()
    await client.post('/api/chat', json={'message':'hello','session_id':'s1'})
    res = await client.delete('/api/sessions/s1'); body = await res.json()
    assert res.status == 502 and body['error'] == 'opencode_delete_failed'
    assert app[SESSION_STORE_KEY].get('s1').deleted is False
    await client.close()


@pytest.mark.asyncio
async def test_delete_session_404_marks_deleted(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=_DeleteStatusClient(404))
    client = TestClient(TestServer(app)); await client.start_server()
    await client.post('/api/chat', json={'message':'hello','session_id':'s1'})
    res = await client.delete('/api/sessions/s1'); body = await res.json()
    assert res.status == 200 and body['success'] is True and body['opencode_missing'] is True
    assert app[SESSION_STORE_KEY].get('s1').deleted is True
    await client.close()

@pytest.mark.asyncio
async def test_clear_sessions_partial_failure_returns_502(tmp_path, monkeypatch):
    class C(FakeOpenCodeClient):
        async def delete_session(self, session_id):
            if session_id.endswith('2'):
                raise OpenCodeClientError('boom', status=500)
            return await super().delete_session(session_id)
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    app = create_app(Settings.from_env(), opencode_client=C())
    client = TestClient(TestServer(app)); await client.start_server()
    await client.post('/api/chat', json={'message':'a','session_id':'s1'})
    await client.post('/api/chat', json={'message':'b','session_id':'s2'})
    res = await client.post('/api/clear'); body = await res.json()
    assert res.status == 502 and body['success'] is False and body['failed_count'] == 1
    assert app[SESSION_STORE_KEY].get('s1').deleted is True
    assert app[SESSION_STORE_KEY].get('s2').deleted is False
    await client.close()


@pytest.mark.asyncio
async def test_delete_session_metadata_exception_does_not_fail_delete(tmp_path, monkeypatch):
    class PM:
        async def publish_session_metadata(self, **kwargs): return {"success": True}
        async def delete_session_metadata(self, session_id): raise RuntimeError("x" * 5000)
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    app[PORTAL_METADATA_CLIENT_KEY] = PM()
    client = TestClient(TestServer(app)); await client.start_server()
    await client.post("/api/chat", json={"message":"hello","session_id":"s1"})
    res = await client.delete("/api/sessions/s1"); body = await res.json()
    assert res.status == 200 and body["success"] is True
    assert body["metadata_delete"]["success"] is False
    assert len(body["metadata_delete"]["error"]) <= 1010
    assert app[SESSION_STORE_KEY].get("s1").deleted is True
    await client.close()


@pytest.mark.asyncio
async def test_delete_session_already_deleted_skips_opencode_calls_metadata_best_effort(tmp_path, monkeypatch):
    class C(FakeOpenCodeClient):
        def __init__(self): super().__init__(); self.delete_calls=0
        async def delete_session(self, session_id): self.delete_calls += 1; return await super().delete_session(session_id)
    class PM:
        def __init__(self): self.calls=0
        async def publish_session_metadata(self, **kwargs): return {"success": True}
        async def delete_session_metadata(self, session_id): self.calls += 1; return {"success": True}
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    c=C(); pm=PM(); app=create_app(Settings.from_env(), opencode_client=c); app[PORTAL_METADATA_CLIENT_KEY]=pm
    client = TestClient(TestServer(app)); await client.start_server()
    await client.post("/api/chat", json={"message":"hello","session_id":"s1"})
    app[SESSION_STORE_KEY].mark_deleted("s1")
    res = await client.delete("/api/sessions/s1"); body = await res.json()
    assert res.status == 200 and body["already_deleted"] is True
    assert c.delete_calls == 0 and pm.calls == 1
    await client.close()


@pytest.mark.asyncio
async def test_delete_session_chatlog_delete_failure_reported_not_fatal(tmp_path, monkeypatch):
    class BadChatlog:
        def delete(self, _sid):
            raise RuntimeError("chatlog boom")

    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    app[CHATLOG_STORE_KEY] = BadChatlog()
    client = TestClient(TestServer(app)); await client.start_server()
    await client.post('/api/chat', json={'message':'hello','session_id':'s1'})
    res = await client.delete('/api/sessions/s1'); body = await res.json()
    assert res.status == 200 and body['success'] is True
    assert body['chatlog_delete']['success'] is False
    assert app[SESSION_STORE_KEY].get('s1').deleted is True
    await client.close()


@pytest.mark.asyncio
async def test_clear_sessions_chatlog_delete_failure_is_reported_not_fatal(tmp_path, monkeypatch):
    class BadChatlog:
        def delete(self, _sid):
            raise RuntimeError("chatlog boom")

    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    app[CHATLOG_STORE_KEY] = BadChatlog()
    client = TestClient(TestServer(app)); await client.start_server()
    await client.post('/api/chat', json={'message':'a','session_id':'s1'})
    res = await client.post('/api/clear'); body = await res.json()
    assert res.status == 200 and body['success'] is True
    assert body['metadata_delete'][0]['chatlog_delete']['success'] is False
    assert app[SESSION_STORE_KEY].get('s1').deleted is True
    await client.close()


@pytest.mark.asyncio
async def test_clear_sessions_partial_opencode_failure_skips_chatlog_and_metadata_for_failed_session(tmp_path, monkeypatch):
    class C(FakeOpenCodeClient):
        async def delete_session(self, session_id):
            if session_id.endswith('3'):
                raise OpenCodeClientError('boom', status=500)
            if session_id.endswith('2'):
                raise OpenCodeClientError('missing', status=404)
            return await super().delete_session(session_id)

    class TrackChatlog:
        def __init__(self): self.calls=[]
        def delete(self, sid): self.calls.append(sid); return True

    class PM:
        def __init__(self): self.calls=[]
        async def publish_session_metadata(self, **kwargs): return {"success": True}
        async def delete_session_metadata(self, sid): self.calls.append(sid); return {"success": True}

    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    c=C(); pm=PM(); chatlog=TrackChatlog()
    app = create_app(Settings.from_env(), opencode_client=c); app[PORTAL_METADATA_CLIENT_KEY]=pm; app[CHATLOG_STORE_KEY]=chatlog
    client = TestClient(TestServer(app)); await client.start_server()
    await client.post('/api/chat', json={'message':'a','session_id':'s1'})
    await client.post('/api/chat', json={'message':'b','session_id':'s2'})
    await client.post('/api/chat', json={'message':'c','session_id':'s3'})
    res = await client.post('/api/clear'); body = await res.json()
    assert res.status == 502 and body['success'] is False and body['deleted_count'] == 2 and body['failed_count'] == 1
    assert app[SESSION_STORE_KEY].get('s1').deleted is True
    assert app[SESSION_STORE_KEY].get('s2').deleted is True
    assert app[SESSION_STORE_KEY].get('s3').deleted is False
    assert set(pm.calls) == {'s1','s2'}
    assert set(chatlog.calls) == {'s1','s2'}
    await client.close()


def test_to_efp_messages_filters_internal_auto_continue_metadata():
    raw = [
        {"id": "msg_user_1", "role": "user", "parts": [{"type": "text", "text": "hi"}]},
        {"id": "msg_internal_1", "role": "user", "parts": [{"type": "text", "text": "continue", "metadata": {"efp_internal": "auto_continue"}}]},
        {"id": "efp-auto-continue-legacy", "role": "user", "parts": [{"type": "text", "text": "legacy"}]},
    ]
    out = _to_efp_messages(raw)
    ids = [m.get("id") for m in out]
    assert "msg_user_1" in ids
    assert "msg_internal_1" not in ids
    assert "efp-auto-continue-legacy" not in ids
