import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.opencode_client import OpenCodeClientError
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
    assert p1["_llm_debug"].get("attachments") == []
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

@pytest.mark.asyncio
async def test_chat_slash_uses_command_api(tmp_path, monkeypatch):
    class SlashClient(FakeOpenCodeClient):
        def __init__(self):
            super().__init__(); self.execute_command_called=0; self.send_message_called=0; self.last_arguments=''
        async def list_commands(self, timeout_seconds=30):
            return [{"name": "java-cucumber-generator"}]
        async def execute_command(self, session_id, *, command, arguments, model, agent, message_id=None):
            self.execute_command_called += 1; self.last_arguments = arguments
            assistant = {"id": "a-1", "role": "assistant", "parts": [{"type": "text", "text": "skill command result"}]}
            self.messages[session_id].append({"id":"u-1","role":"user","parts":[{"type":"text","text":"/x"}]}); self.messages[session_id].append(assistant)
            return {"message": assistant}
        async def send_message(self, *args, **kwargs):
            self.send_message_called += 1
            raise AssertionError('send_message should not be called')

    state = tmp_path / 'state'; state.mkdir(parents=True)
    (state / 'skills-index.json').write_text(json.dumps({"skills":[{"efp_name":"java-cucumber-generator","opencode_name":"java-cucumber-generator","description":"Generate Java Cucumber scaffolding","opencode_supported":True,"runtime_equivalence":True,"programmatic":False,"missing_tools":[],"missing_opencode_tools":[]}]}), encoding='utf-8')
    cfg = tmp_path / 'opencode.json'; cfg.write_text(json.dumps({"permission":{"skill":{"java-cucumber-generator":"allow"}}}), encoding='utf-8')
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(state)); monkeypatch.setenv('OPENCODE_CONFIG', str(cfg))
    fake = SlashClient(); app = create_app(Settings.from_env(), opencode_client=fake); client = TestClient(TestServer(app)); await client.start_server()
    r = await client.post('/api/chat', json={"message":"/java-cucumber-generator hello world","session_id":"s1"}); p = await r.json()
    assert r.status == 200 and p['response'] == 'skill command result'
    assert fake.execute_command_called == 1 and fake.send_message_called == 0 and fake.last_arguments == 'hello world'
    assert any(e['type']=='skill.detected' for e in p['runtime_events']) and any(e['type']=='skill.command.executed' for e in p['runtime_events'])
    assert p['_llm_debug']['skill_invocation']['used_command_api'] is True
    await client.close()

@pytest.mark.asyncio
async def test_chat_slash_blocked_programmatic(tmp_path, monkeypatch):
    class C(FakeOpenCodeClient):
        async def list_commands(self, timeout_seconds=30): return []
        async def execute_command(self, *args, **kwargs): raise AssertionError()
        async def send_message(self, *args, **kwargs): raise AssertionError()
    state=tmp_path/'state'; state.mkdir(parents=True)
    (state/'skills-index.json').write_text(json.dumps({"skills":[{"efp_name":"java-cucumber-generator","opencode_name":"java-cucumber-generator","opencode_supported":True,"runtime_equivalence":False,"programmatic":True,"missing_tools":[],"missing_opencode_tools":[]}]}), encoding='utf-8')
    cfg=tmp_path/'opencode.json'; cfg.write_text(json.dumps({"permission":{"skill":{"java-cucumber-generator":"allow"}}}), encoding='utf-8')
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(state)); monkeypatch.setenv('OPENCODE_CONFIG', str(cfg))
    client=TestClient(TestServer(create_app(Settings.from_env(), opencode_client=C()))); await client.start_server()
    r=await client.post('/api/chat', json={"message":"/java-cucumber-generator hello","session_id":"s2"}); p=await r.json()
    assert r.status==200 and 'programmatic_skill_requires_opencode_wrapper' in p['response']
    assert any(e['type']=='skill.blocked' for e in p['runtime_events'])
    await client.close()


@pytest.mark.asyncio
async def test_chat_slash_fallback_prompt(tmp_path, monkeypatch):
    class C(FakeOpenCodeClient):
        def __init__(self): super().__init__(); self.parts=None
        async def list_commands(self, timeout_seconds=30): return []
        async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
            self.parts=parts
            return await super().send_message(session_id, parts=parts, model=model, agent=agent, system=system, message_id=message_id, no_reply=no_reply, tools=tools)
    state=tmp_path/'state'; state.mkdir(parents=True)
    (state/'skills-index.json').write_text(json.dumps({"skills":[{"efp_name":"java-cucumber-generator","opencode_name":"java-cucumber-generator","opencode_supported":True,"runtime_equivalence":True,"programmatic":False,"missing_tools":[],"missing_opencode_tools":[]}]}), encoding='utf-8')
    cfg=tmp_path/'opencode.json'; cfg.write_text(json.dumps({"permission":{"skill":{"java-cucumber-generator":"allow"}}}), encoding='utf-8')
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(state)); monkeypatch.setenv('OPENCODE_CONFIG', str(cfg))
    fake=C(); client=TestClient(TestServer(create_app(Settings.from_env(), opencode_client=fake))); await client.start_server()
    r=await client.post('/api/chat', json={"message":"/java-cucumber-generator hello world","session_id":"s3"}); p=await r.json()
    assert r.status==200


@pytest.mark.asyncio
async def test_chat_waits_past_progress_text_for_final_answer(tmp_path, monkeypatch):
    class ProgressThenFinal(FakeOpenCodeClient):
        def __init__(self):
            super().__init__(); self.calls = 0
        async def send_message(self, session_id, **kwargs):
            self.messages[session_id].append({"id":"u-1","role":"user","parts":[{"type":"text","text":"q"}]})
            self.messages[session_id].append({"id":"a-1","role":"assistant","parts":[{"type":"text","text":"I am fetching the Confluence page now and will summarize the agenda once I have the content"},{"type":"tool","status":"running"}]})
            return {"message": self.messages[session_id][-1]}
        async def list_messages(self, session_id):
            self.calls += 1
            if self.calls >= 2 and not any(m.get("id") == "a-2" for m in self.messages[session_id]):
                self.messages[session_id].append({"id":"a-2","role":"assistant","finish_reason":"stop","parts":[{"type":"text","text":"Agenda summary ..."}]})
            return list(self.messages[session_id])
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=ProgressThenFinal()); client = TestClient(TestServer(app)); await client.start_server()
    r = await client.post("/api/chat", json={"message":"q","session_id":"s-progress-1"}); p = await r.json()
    assert p["ok"] is True and p["completion_state"] == "completed" and p["response"] == "Agenda summary ..."
    await client.close()


@pytest.mark.asyncio
async def test_chat_unknown_slash_uses_native_opencode_command(tmp_path, monkeypatch):
    class C(FakeOpenCodeClient):
        def __init__(self):
            super().__init__()
            self.execute_command_called = 0
            self.send_message_called = 0
        async def list_commands(self, timeout_seconds=30):
            return [{"name": "native-command"}]
        async def execute_command(self, session_id, *, command, arguments, model, agent, message_id=None):
            self.execute_command_called += 1
            assistant = {"id": "a-1", "role": "assistant", "parts": [{"type": "text", "text": "native ok"}]}
            self.messages[session_id].extend([{"id": "u-1", "role": "user", "parts": [{"type": "text", "text": "/native-command hello world"}]}, assistant])
            return {"message": assistant}
        async def send_message(self, *args, **kwargs):
            self.send_message_called += 1
            raise AssertionError()
    state = tmp_path / "state"; state.mkdir(parents=True)
    (state / "skills-index.json").write_text(json.dumps({"skills": []}), encoding="utf-8")
    cfg = tmp_path / "opencode.json"; cfg.write_text(json.dumps({"permission": {"skill": {"*": "deny"}}}), encoding="utf-8")
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state)); monkeypatch.setenv("OPENCODE_CONFIG", str(cfg))
    fake = C(); client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=fake))); await client.start_server()
    resp = await client.post("/api/chat", json={"message": "/native-command hello world", "session_id": "s-native"})
    payload = await resp.json()
    assert resp.status == 200 and payload["response"] == "native ok"
    assert fake.execute_command_called == 1 and fake.send_message_called == 0
    assert payload["_llm_debug"]["skill_invocation"]["kind"] == "command"
    assert payload["_llm_debug"]["skill_invocation"]["native_command"] is True
    assert any(e["type"] == "skill.command.executed" for e in payload["runtime_events"])
    assert any(e["type"] == "skill.completed" for e in payload["runtime_events"])
    await client.close()


@pytest.mark.asyncio
async def test_chat_unknown_slash_blocks_when_no_skill_or_command(tmp_path, monkeypatch):
    class C(FakeOpenCodeClient):
        def __init__(self):
            super().__init__()
            self.execute_command_called = 0
            self.send_message_called = 0
        async def list_commands(self, timeout_seconds=30): return []
        async def execute_command(self, *args, **kwargs): self.execute_command_called += 1
        async def send_message(self, *args, **kwargs): self.send_message_called += 1
    state = tmp_path / "state"; state.mkdir(parents=True)
    (state / "skills-index.json").write_text(json.dumps({"skills": []}), encoding="utf-8")
    cfg = tmp_path / "opencode.json"; cfg.write_text(json.dumps({"permission": {"skill": {"*": "deny"}}}), encoding="utf-8")
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state)); monkeypatch.setenv("OPENCODE_CONFIG", str(cfg))
    fake = C(); client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=fake))); await client.start_server()
    payload = await (await client.post("/api/chat", json={"message": "/unknown-cmd x", "session_id": "s-unknown"})).json()
    assert "unknown_skill_or_command" in payload["response"]
    assert fake.send_message_called == 0 and fake.execute_command_called == 0
    await client.close()


@pytest.mark.asyncio
async def test_chat_allowed_skill_falls_back_to_prompt_when_list_commands_fails(tmp_path, monkeypatch):
    class C(FakeOpenCodeClient):
        def __init__(self): super().__init__(); self.parts = None
        async def list_commands(self, timeout_seconds=30): raise OpenCodeClientError("command list down")
        async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
            self.parts = parts
            return await super().send_message(session_id, parts=parts, model=model, agent=agent, system=system, message_id=message_id, no_reply=no_reply, tools=tools)
    state = tmp_path / "state"; state.mkdir(parents=True)
    (state / "skills-index.json").write_text(json.dumps({"skills": [{"efp_name": "k", "opencode_name": "k", "opencode_supported": True, "runtime_equivalence": True, "programmatic": False, "missing_tools": [], "missing_opencode_tools": []}]}), encoding="utf-8")
    cfg = tmp_path / "opencode.json"; cfg.write_text(json.dumps({"permission": {"skill": {"k": "allow"}}}), encoding="utf-8")
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state)); monkeypatch.setenv("OPENCODE_CONFIG", str(cfg))
    fake = C(); client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=fake))); await client.start_server()
    r = await client.post("/api/chat", json={"message": "/k arg", "session_id": "s-k"}); payload = await r.json()
    assert r.status == 200
    assert "Use the native OpenCode `skill` tool" in fake.parts[0]["text"]
    assert payload["_llm_debug"]["skill_invocation"]["command_lookup_error"]
    await client.close()


@pytest.mark.asyncio
async def test_chat_slash_blocked_updates_metadata_and_session(tmp_path, monkeypatch):
    class C(FakeOpenCodeClient):
        async def list_commands(self, timeout_seconds=30): return []
        async def execute_command(self, *args, **kwargs): raise AssertionError()
        async def send_message(self, *args, **kwargs): raise AssertionError()
    state = tmp_path / "state"; state.mkdir(parents=True)
    (state / "skills-index.json").write_text(json.dumps({"skills": [{"efp_name": "p", "opencode_name": "p", "opencode_supported": True, "runtime_equivalence": False, "programmatic": True, "missing_tools": [], "missing_opencode_tools": []}]}), encoding="utf-8")
    cfg = tmp_path / "opencode.json"; cfg.write_text(json.dumps({"permission": {"skill": {"p": "allow"}}}), encoding="utf-8")
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state)); monkeypatch.setenv("OPENCODE_CONFIG", str(cfg))
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=C()))); await client.start_server()
    resp = await client.post("/api/chat", json={"message": "/p hi", "session_id": "s-block"}); payload = await resp.json()
    assert payload["context_state"]["current_state"] == "blocked"
    chatlog = await (await client.get("/api/sessions/s-block/chatlog")).json()
    assert chatlog["context_state"]["current_state"] == "blocked"
    assert chatlog["status"] == "blocked"
    await client.close()


@pytest.mark.asyncio
async def test_skill_blocked_returns_blocked_completion_state(tmp_path, monkeypatch):
    class C(FakeOpenCodeClient):
        async def list_commands(self, timeout_seconds=30): return []
        async def execute_command(self, *args, **kwargs): raise AssertionError()
        async def send_message(self, *args, **kwargs): raise AssertionError()
    state = tmp_path / "state"; state.mkdir(parents=True)
    (state / "skills-index.json").write_text(json.dumps({"skills": []}), encoding="utf-8")
    cfg = tmp_path / "opencode.json"; cfg.write_text(json.dumps({"permission": {"skill": {"*": "deny"}}}), encoding="utf-8")
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state)); monkeypatch.setenv("OPENCODE_CONFIG", str(cfg))
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=C()))); await client.start_server()
    payload = await (await client.post("/api/chat", json={"message": "/unknown hi", "session_id": "s-skill-blocked"})).json()
    assert payload["ok"] is False
    assert payload["completion_state"] == "blocked"
    assert payload["incomplete_reason"]
    assert "Skill `" in payload["response"]
    chatlog = await (await client.get("/api/sessions/s-skill-blocked/chatlog")).json()
    assert chatlog["status"] != "success"
    await client.close()


@pytest.mark.asyncio
async def test_chat_does_not_success_on_progress_timeout(tmp_path, monkeypatch):
    class ProgressOnly(FakeOpenCodeClient):
        async def send_message(self, session_id, **kwargs):
            msg = {"id": "a1", "role": "assistant", "parts": [{"type": "text", "text": "I am fetching the Confluence page now and will summarize the agenda once I have the content"}, {"type": "tool", "status": "running"}]}
            self.messages[session_id].append(msg); return {"message": msg}
        async def list_messages(self, session_id): return list(self.messages[session_id])
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_POLL_SECONDS", "0.01")
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=ProgressOnly()))); await client.start_server()
    payload = await (await client.post("/api/chat", json={"message": "q", "session_id": "s-timeout"})).json()
    assert payload["ok"] is False
    assert payload["completion_state"] in {"incomplete", "blocked"}
    assert "I am fetching" not in payload["response"]
    assert not any(e.get("type") == "complete" for e in payload["runtime_events"])
    assert payload["_llm_debug"]["completion_probe"]["reason"] in {"final_assistant_message_timeout", "before_snapshot_unreliable"}
    await client.close()


@pytest.mark.asyncio
async def test_chat_returns_error_on_tool_failure_before_final(tmp_path, monkeypatch):
    class ToolError(FakeOpenCodeClient):
        async def send_message(self, session_id, **kwargs):
            msg = {"id": "a1", "role": "assistant", "parts": [{"type": "tool", "status": "error", "error": "boom"}]}
            self.messages[session_id].append(msg); return {"message": msg}
        async def list_messages(self, session_id): return list(self.messages[session_id])
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=ToolError()))); await client.start_server()
    payload = await (await client.post("/api/chat", json={"message": "q", "session_id": "s-error"})).json()
    assert payload["ok"] is False
    assert payload["completion_state"] == "error"
    assert "tool execution failed" in payload["response"].lower()
    chatlog = await (await client.get("/api/sessions/s-error/chatlog")).json()
    assert chatlog["status"] != "success"
    assert not any(e.get("type") == "complete" for e in payload["runtime_events"])
    await client.close()


@pytest.mark.asyncio
async def test_chat_does_not_use_old_history_when_before_messages_unavailable(tmp_path, monkeypatch):
    class BeforeUnavailable(FakeOpenCodeClient):
        def __init__(self):
            super().__init__(); self.calls = 0
        async def send_message(self, session_id, **kwargs):
            self.messages[session_id] = [{"id": "old-a", "role": "assistant", "finish_reason": "stop", "parts": [{"type": "text", "text": "OLD FINAL"}]}, {"id": "new-a", "role": "assistant", "parts": [{"type": "text", "text": "I am fetching the Confluence page now and will summarize the agenda once I have the content"}, {"type": "tool", "status": "running"}]}]
            return {"message": self.messages[session_id][-1]}
        async def list_messages(self, session_id):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("cannot list before")
            return list(self.messages[session_id])
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "0.02")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_POLL_SECONDS", "0.01")
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=BeforeUnavailable()))); await client.start_server()
    payload = await (await client.post("/api/chat", json={"message": "q", "session_id": "s-old"})).json()
    assert payload["ok"] is False
    assert payload["completion_state"] == "incomplete"
    assert payload["response"] != "OLD FINAL"
    await client.close()
