import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


@pytest.mark.asyncio
async def test_chat_metadata_must_be_object(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    res = await client.post("/api/chat", json={"message": "hello", "metadata": "bad"})
    payload = await res.json()
    assert res.status == 400
    assert payload["error"] == "metadata_must_be_object"
    await client.close()


@pytest.mark.asyncio
async def test_chat_stream_metadata_must_be_object(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    res = await client.post("/api/chat/stream", json={"message": "hello", "metadata": "bad"})
    body = await res.text()
    assert res.status == 200
    assert "event: error" in body
    assert "metadata_must_be_object" in body
    await client.close()


@pytest.mark.asyncio
async def test_adapter_restart_mapping_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = FakeOpenCodeClient()

    app1 = create_app(Settings.from_env(), opencode_client=fake)
    client1 = TestClient(TestServer(app1))
    await client1.start_server()
    r1 = await client1.post("/api/chat", json={"message": "hello", "session_id": "portal-1"})
    p1 = await r1.json()
    op1 = p1["_llm_debug"]["opencode_session_id"]
    await client1.close()

    app2 = create_app(Settings.from_env(), opencode_client=fake)
    client2 = TestClient(TestServer(app2))
    await client2.start_server()
    r2 = await client2.post("/api/chat", json={"message": "again", "session_id": "portal-1"})
    p2 = await r2.json()

    assert p2["_llm_debug"]["opencode_session_id"] == op1
    assert fake.create_calls == 1
    await client2.close()


@pytest.mark.asyncio
async def test_explicit_partial_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = FakeOpenCodeClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()

    first = await client.post("/api/chat", json={"message": "hello", "session_id": "portal-1"})
    p1 = await first.json()
    old_op = p1["_llm_debug"]["opencode_session_id"]

    fake.sessions.pop(old_op, None)
    fake.messages.pop(old_op, None)

    second = await client.post("/api/chat", json={"message": "again", "session_id": "portal-1"})
    p2 = await second.json()

    assert second.status == 200
    assert p2["session_id"] == "portal-1"
    assert p2["_llm_debug"].get("partial_recovery") is True
    assert p2["_llm_debug"]["opencode_session_id"] != old_op
    await client.close()


@pytest.mark.asyncio
async def test_chat_invalid_json_returns_json_400(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    res = await client.post("/api/chat", data='{"message":', headers={"Content-Type": "application/json"})
    payload = await res.json()
    assert res.status == 400
    assert payload["error"] == "invalid_json"
    await client.close()


@pytest.mark.asyncio
async def test_chat_stream_invalid_json_emits_sse_error(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    res = await client.post("/api/chat/stream", data='{"message":', headers={"Content-Type": "application/json"})
    body = await res.text()
    assert res.status == 200
    assert "event: error" in body
    assert "invalid_json" in body
    await client.close()


@pytest.mark.asyncio
async def test_chat_metadata_list_returns_400(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    res = await client.post("/api/chat", json={"message": "hello", "metadata": []})
    payload = await res.json()

    assert res.status == 400
    assert payload["error"] == "metadata_must_be_object"
    await client.close()


@pytest.mark.asyncio
async def test_chat_runtime_profile_must_be_object(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    res = await client.post("/api/chat", json={"message": "hello", "metadata": {"runtime_profile": "bad"}})
    payload = await res.json()

    assert res.status == 400
    assert payload["error"] == "runtime_profile_must_be_object"
    await client.close()


@pytest.mark.asyncio
async def test_chat_non_string_title_name_falls_back_without_500(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    res = await client.post(
        "/api/chat",
        json={"message": "hello title fallback", "metadata": {"title": ["bad"], "name": {"bad": "x"}}},
    )
    payload = await res.json()

    assert res.status == 200
    assert payload["response"]
    sessions = await (await client.get("/api/sessions")).json()
    assert sessions["sessions"][0]["name"].startswith("hello title fallback")
    await client.close()


@pytest.mark.asyncio
async def test_chat_stream_runtime_profile_must_be_object_emits_error(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    res = await client.post("/api/chat/stream", json={"message": "hello", "metadata": {"runtime_profile": "bad"}})
    body = await res.text()

    assert res.status == 200
    assert "text/event-stream" in res.headers.get("Content-Type", "")
    assert "event: runtime_event" in body
    assert "event: error" in body
    assert "runtime_profile_must_be_object" in body
    await client.close()


@pytest.mark.asyncio
async def test_chat_session_id_must_be_string(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    res = await client.post("/api/chat", json={"message": "hello", "session_id": ["bad"]})
    payload = await res.json()

    assert res.status == 400
    assert payload["error"] == "session_id_must_be_string"
    await client.close()


@pytest.mark.asyncio
async def test_chat_session_id_dict_returns_400(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    res = await client.post("/api/chat", json={"message": "hello", "session_id": {"bad": "x"}})
    payload = await res.json()
    assert res.status == 400
    assert payload["error"] == "session_id_must_be_string"
    await client.close()


@pytest.mark.asyncio
async def test_chat_request_id_must_be_string(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    res = await client.post("/api/chat", json={"message": "hello", "request_id": ["bad"]})
    payload = await res.json()
    assert res.status == 400
    assert payload["error"] == "request_id_must_be_string"
    await client.close()


@pytest.mark.asyncio
async def test_chat_empty_session_and_request_ids_are_generated(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    res = await client.post("/api/chat", json={"message": "hello", "session_id": "   ", "request_id": "   "})
    payload = await res.json()

    assert res.status == 200
    assert isinstance(payload["session_id"], str)
    assert payload["session_id"].strip()
    assert isinstance(payload["request_id"], str)
    assert payload["request_id"].startswith("chat-")
    await client.close()


@pytest.mark.asyncio
async def test_chat_explicit_request_id_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    res = await client.post("/api/chat", json={"message": "hello", "request_id": "req-1"})
    payload = await res.json()
    assert res.status == 200
    assert payload["request_id"] == "req-1"
    await client.close()


@pytest.mark.asyncio
async def test_chat_stream_session_id_must_be_string_emits_error(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    res = await client.post("/api/chat/stream", json={"message": "hello", "session_id": ["bad"]})
    body = await res.text()

    assert res.status == 200
    assert "text/event-stream" in res.headers.get("Content-Type", "")
    assert "event: error" in body
    assert "session_id_must_be_string" in body
    await client.close()


@pytest.mark.asyncio
async def test_chat_stream_request_id_must_be_string_emits_error(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    res = await client.post("/api/chat/stream", json={"message": "hello", "request_id": ["bad"]})
    body = await res.text()

    assert res.status == 200
    assert "event: error" in body
    assert "request_id_must_be_string" in body
    await client.close()
