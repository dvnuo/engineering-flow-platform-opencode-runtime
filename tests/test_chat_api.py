import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


@pytest.mark.asyncio
async def test_chat_and_stream(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PORTAL_AGENT_ID", "agent-obs-1")
    fake = FakeOpenCodeClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()

    r1 = await client.post("/api/chat", json={"message": "hello", "session_id": "sess-obs-1", "request_id": "req-obs-1", "metadata": {"runtime_profile_id": "rp-1", "runtime_profile": {"revision": 7, "provider": "test-provider", "model": "test-model"}}})
    assert r1.status == 200
    p1 = await r1.json()
    assert p1["session_id"]
    assert p1["request_id"]
    assert p1["response"] == "echo: hello"
    assert p1["user_message_id"].startswith("u-")
    assert p1["assistant_message_id"].startswith("a-")
    assert p1["_llm_debug"]["engine"] == "opencode"
    assert p1["_llm_debug"]["opencode_session_id"]
    tc = p1["_llm_debug"]["trace_context"]
    assert tc["agent_id"] == "agent-obs-1"
    assert tc["runtime_type"] == "opencode"
    assert tc["session_id"] == "sess-obs-1"
    assert tc["request_id"] == "req-obs-1"
    assert tc["profile_version"] == "7"
    assert tc["runtime_profile_id"] == "rp-1"
    assert tc["trace_id"] == "req-obs-1"
    for evt in p1["runtime_events"]:
        assert evt["trace_context"]
        assert evt["data"]["trace_context"]
        assert evt["agent_id"] == "agent-obs-1"
        assert evt["runtime_type"] == "opencode"
        assert evt["trace_id"] == "req-obs-1"

    index = tmp_path / "state" / "sessions" / "index.json"
    assert index.exists()

    sid = p1["session_id"]
    assert p1["runtime_events"]
    assert any(e["type"] == "execution.started" for e in p1["runtime_events"])
    assert any(e["type"] == "llm_thinking" for e in p1["runtime_events"])
    assert any(e["type"] == "complete" for e in p1["runtime_events"])
    assert any(e["type"] == "execution.completed" for e in p1["runtime_events"])
    assert p1["usage"]["requests"] == 1
    assert p1["context_state"]["summary"]

    chatlog_resp = await client.get(f"/api/sessions/{sid}/chatlog")
    chatlog = await chatlog_resp.json()
    assert chatlog["success"] is True
    assert chatlog["chatlog"]["entries"]
    assert chatlog["runtime_events"]
    assert chatlog["request_id"]
    assert chatlog["status"] == "success"
    assert chatlog["chatlog"]["entries"][-1]["status"] == "success"
    assert chatlog["chatlog"]["entries"][-1]["response"] == "echo: hello"
    assert chatlog["context_state"]["current_state"] == "completed"
    assert chatlog["llm_debug"]["usage"]["requests"] == 1

    chatlog_types = {e["type"] for e in chatlog["runtime_events"]}
    assert "execution.started" in chatlog_types
    assert "llm_thinking" in chatlog_types
    assert "complete" in chatlog_types
    assert "execution.completed" in chatlog_types
    op_sid = p1["_llm_debug"]["opencode_session_id"]
    r2 = await client.post("/api/chat", json={"message": "again", "session_id": sid})
    p2 = await r2.json()
    assert p2["_llm_debug"]["opencode_session_id"] == op_sid
    assert fake.create_calls == 1

    r3 = await client.post("/api/chat", json={"message": "x", "session_id": "portal-1"})
    assert (await r3.json())["session_id"] == "portal-1"

    r4 = await client.post("/api/chat", json={"message": ""})
    assert r4.status == 400

    rs = await client.post("/api/chat/stream", json={"message": "hello stream"})
    body = await rs.text()
    assert rs.status == 200
    assert "text/event-stream" in rs.headers.get("Content-Type", "")
    assert "event: runtime_event" in body
    assert "event: final" in body
    assert "event: done" in body
    assert body.index("event: runtime_event") < body.index("event: final")
    r_secret = await client.post("/api/chat", json={"message": "secret", "session_id": "sess-obs-2", "request_id": "token-should-not-leak"})
    p_secret = await r_secret.json()
    assert "token-should-not-leak" not in json.dumps(p_secret["runtime_events"]).lower()
    assert "token-should-not-leak" not in json.dumps(p_secret["_llm_debug"]).lower()
    await client.close()


@pytest.mark.asyncio
async def test_chat_stream_final_contract_contains_response_and_done_is_json_marker(tmp_path, monkeypatch):
    class ContractFakeOpenCodeClient(FakeOpenCodeClient):
        async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
            user_text = parts[0].get("text", "")
            user = {"id": f"u-{len(self.messages[session_id])+1}", "role": "user", "parts": [{"type": "text", "text": user_text}]}
            assistant = {"id": f"a-{len(self.messages[session_id])+2}", "role": "assistant", "parts": [{"type": "text", "text": "hello from opencode"}]}
            self.messages[session_id].extend([user, assistant])
            return {"message": assistant, "usage": {"input_tokens": 10, "output_tokens": 5, "cost": 0.001}, "model": model or "test-model", "provider": "test-provider"}

    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = ContractFakeOpenCodeClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post(
            "/api/chat/stream",
            json={
                "message": "hi",
                "session_id": "s-opencode-contract",
                "request_id": "r-opencode-contract",
            },
        )
        body = await resp.text()
        assert resp.status == 200
        assert "event: final" in body
        assert "event: done" in body
        assert body.index("event: final") < body.index("event: done")
        assert "event: done\ndata: \n\n" not in body

        events = []
        for chunk in body.strip().split("\n\n"):
            event_name = None
            data_line = None
            for line in chunk.splitlines():
                if line.startswith("event: "):
                    event_name = line.removeprefix("event: ")
                elif line.startswith("data: "):
                    data_line = line.removeprefix("data: ")
            if event_name is not None and data_line is not None:
                events.append((event_name, json.loads(data_line)))

        final_data = next(payload for event_name, payload in events if event_name == "final")
        assert final_data["response"] == "hello from opencode"
        assert final_data["session_id"] == "s-opencode-contract"
        assert final_data["request_id"] == "r-opencode-contract"

        done_data = next(payload for event_name, payload in events if event_name == "done")
        assert done_data["ok"] is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_chat_response_uses_history_assistant_text_not_user_input(tmp_path, monkeypatch):
    class UserOnlyFirst(FakeOpenCodeClient):
        async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
            user_text = parts[0].get("text", "")
            self.messages[session_id].append({"id": "u-1", "role": "user", "parts": [{"type": "text", "text": user_text}]})
            self.messages[session_id].append({"id": "a-1", "role": "assistant", "parts": [{"type": "reasoning", "text": "hidden"}, {"type": "text", "text": "Hi. What do you need?"}, {"type": "step-finish", "reason": "stop"}]})
            return {"messages": [{"id": "u-1", "role": "user", "parts": [{"type": "text", "text": user_text}]}]}

    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=UserOnlyFirst())
    client = TestClient(TestServer(app))
    await client.start_server()
    resp = await client.post("/api/chat", json={"message": "HI", "session_id": "s-visible-1"})
    payload = await resp.json()
    assert payload["response"] == "Hi. What do you need?"
    session = await (await client.get("/api/sessions/s-visible-1")).json()
    assert session["messages"][-1]["content"] == "Hi. What do you need?"
    assert "hidden" not in session["messages"][-1]["content"]
    await client.close()


@pytest.mark.asyncio
async def test_chat_handles_malformed_usage_payload_without_500(tmp_path, monkeypatch):
    class MalformedUsageClient(FakeOpenCodeClient):
        async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
            assistant = {"role": "assistant", "parts": [{"type": "text", "text": "ok"}]}
            self.messages[session_id].append({"role": "user", "parts": [{"type": "text", "text": parts[0].get("text", "")}]})
            self.messages[session_id].append(assistant)
            return {"message": assistant, "usage": {"input_tokens": "not-number", "output_tokens": None, "cost": "bad"}}

    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=MalformedUsageClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    res = await client.post("/api/chat", json={"message": "hello", "session_id": "s-usage-bad"})
    body = await res.json()
    assert res.status == 200
    assert body["response"] == "ok"
    assert body["usage"]["input_tokens"] == 0
    assert body["usage"]["output_tokens"] == 0
    assert body["usage"]["cost"] == 0.0
    await client.close()
