import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.opencode_client import OpenCodeClientError
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.app_keys import SESSION_STORE_KEY
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
    assert p1["assistant_message_ids"] == [p1["assistant_message_id"]]
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
async def test_slash_skill_with_missing_tools_uses_skill_prompt_instead_of_blocking(tmp_path, monkeypatch):
    class C(FakeOpenCodeClient):
        def __init__(self):
            super().__init__()
            self.parts = None
            self.execute_command_called = 0

        async def list_commands(self, timeout_seconds=30):
            return []

        async def execute_command(self, *args, **kwargs):
            self.execute_command_called += 1
            raise AssertionError()

        async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
            self.parts = parts
            return await super().send_message(session_id, parts=parts, model=model, agent=agent, system=system, message_id=message_id, no_reply=no_reply, tools=tools)

    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "skills-index.json").write_text(json.dumps({"skills": [{"efp_name": "demo_skill", "opencode_name": "demo-skill", "opencode_supported": True, "runtime_equivalence": True, "programmatic": False, "missing_tools": ["github_review_writeback"], "missing_opencode_tools": []}]}), encoding="utf-8")
    cfg = tmp_path / "opencode.json"
    cfg.write_text(json.dumps({"permission": {"skill": {"*": "allow"}}}), encoding="utf-8")
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(cfg))

    fake = C()
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=fake)))
    await client.start_server()
    resp = await client.post("/api/chat", json={"message": "/demo-skill review this PR", "session_id": "s-missing-tools"})
    result = await resp.json()

    assert result["ok"] is True
    assert result["completion_state"] == "completed"
    assert "missing_required_tools" not in result["response"]
    assert "cannot run in OpenCode runtime" not in result["response"]

    skill_debug = result["_llm_debug"]["skill_invocation"]
    assert skill_debug["reason"] == "allowed_with_missing_tools"
    assert skill_debug["blocked"] is False
    assert skill_debug["used_skill_prompt"] is True
    assert skill_debug["used_command_api"] is False

    sent_text = fake.parts[0]["text"]
    assert "Compatibility warning" in sent_text
    assert "github_review_writeback" in sent_text
    assert "Still load and apply the skill as far as possible" in sent_text
    assert "Do not replace missing writeback/API tools with raw curl" in sent_text

    event_types = {e.get("type") or e.get("event_type") for e in result["runtime_events"]}
    assert "skill.blocked" not in event_types
    assert "skill.prompt_applied" in event_types
    assert fake.execute_command_called == 0
    await client.close()


@pytest.mark.asyncio
async def test_slash_skill_permission_denied_still_blocks(tmp_path, monkeypatch):
    class C(FakeOpenCodeClient):
        def __init__(self):
            super().__init__()
            self.execute_command_called = 0
            self.send_message_called = 0

        async def list_commands(self, timeout_seconds=30):
            return []

        async def execute_command(self, *args, **kwargs):
            self.execute_command_called += 1
            raise AssertionError()

        async def send_message(self, *args, **kwargs):
            self.send_message_called += 1
            raise AssertionError()

    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "skills-index.json").write_text(json.dumps({"skills": [{"efp_name": "demo_skill", "opencode_name": "demo-skill", "opencode_supported": True, "runtime_equivalence": True, "programmatic": False, "missing_tools": [], "missing_opencode_tools": []}]}), encoding="utf-8")
    cfg = tmp_path / "opencode.json"
    cfg.write_text(json.dumps({"permission": {"skill": {"*": "deny"}}}), encoding="utf-8")
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(cfg))

    fake = C()
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=fake)))
    await client.start_server()
    resp = await client.post("/api/chat", json={"message": "/demo-skill hi", "session_id": "s-denied-skill"})
    result = await resp.json()

    assert result["ok"] is False
    assert result["completion_state"] == "blocked"
    assert result["incomplete_reason"] == "permission_denied"
    assert "cannot run in OpenCode runtime: permission_denied" in result["response"]
    event_types = {e.get("type") or e.get("event_type") for e in result["runtime_events"]}
    assert "skill.blocked" in event_types
    assert fake.send_message_called == 0
    assert fake.execute_command_called == 0
    await client.close()



@pytest.mark.asyncio
async def test_chat_slash_known_skill_command_api_failure_falls_back_to_skill_prompt(tmp_path, monkeypatch):
    class C(FakeOpenCodeClient):
        def __init__(self):
            super().__init__()
            self.execute_command_called = 0
            self.send_message_called = 0
            self.parts = None

        async def list_commands(self, timeout_seconds=30):
            return [{"name": "test-scenarios-design"}]

        async def execute_command(self, *args, **kwargs):
            self.execute_command_called += 1
            raise OpenCodeClientError("POST /session/sid/command failed with status 400:", status=400)

        async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
            self.send_message_called += 1
            self.parts = parts
            return await super().send_message(session_id, parts=parts, model=model, agent=agent, system=system, message_id=message_id, no_reply=no_reply, tools=tools)

    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "skills-index.json").write_text(json.dumps({"skills": [{"efp_name": "test_scenarios_design", "opencode_name": "test-scenarios-design", "opencode_supported": True, "runtime_equivalence": True, "programmatic": False, "missing_tools": [], "missing_opencode_tools": []}]}), encoding="utf-8")
    cfg = tmp_path / "opencode.json"
    cfg.write_text(json.dumps({"permission": {"skill": {"*": "allow"}}}), encoding="utf-8")
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(cfg))

    fake = C()
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=fake)))
    await client.start_server()
    response = await client.post("/api/chat", json={"message": "/test-scenarios-design HKD to USD", "session_id": "s-command-fallback"})
    result = await response.json()

    assert response.status == 200
    assert result["ok"] is True
    assert result["completion_state"] == "completed"
    assert "opencode_error" not in json.dumps(result)
    assert "command failed with status 400" not in result["response"]

    skill_debug = result["_llm_debug"]["skill_invocation"]
    assert skill_debug["reason"] == "allowed"
    assert skill_debug["command_api_fallback"] is True
    assert "command_execution_error" in skill_debug
    assert skill_debug["used_skill_prompt"] is True
    assert skill_debug["used_command_api"] is False

    assert fake.execute_command_called == 1
    assert fake.send_message_called == 1
    assert "Use the native OpenCode `skill` tool" in fake.parts[0]["text"]
    assert "test-scenarios-design" in fake.parts[0]["text"]
    assert "HKD to USD" in fake.parts[0]["text"]

    event_types = {e.get("type") or e.get("event_type") for e in result["runtime_events"]}
    assert "skill.command.failed" in event_types
    assert "skill.prompt_applied" in event_types
    assert "skill.command.executed" not in event_types
    assert "skill.blocked" not in event_types
    assert "execution.failed" not in event_types
    await client.close()


@pytest.mark.asyncio
async def test_chat_unknown_native_command_execution_failure_returns_blocked_not_bad_gateway(tmp_path, monkeypatch):
    class C(FakeOpenCodeClient):
        def __init__(self):
            super().__init__()
            self.execute_command_called = 0
            self.send_message_called = 0

        async def list_commands(self, timeout_seconds=30):
            return [{"name": "native-command"}]

        async def execute_command(self, *args, **kwargs):
            self.execute_command_called += 1
            raise OpenCodeClientError("POST /session/sid/command failed with status 400:", status=400)

        async def send_message(self, *args, **kwargs):
            self.send_message_called += 1
            raise AssertionError()

    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "skills-index.json").write_text(json.dumps({"skills": []}), encoding="utf-8")
    cfg = tmp_path / "opencode.json"
    cfg.write_text(json.dumps({"permission": {"skill": {"*": "allow"}}}), encoding="utf-8")
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(cfg))

    fake = C()
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=fake)))
    await client.start_server()
    response = await client.post("/api/chat", json={"message": "/native-command arg", "session_id": "s-native-cmd-fail"})
    result = await response.json()

    assert response.status == 200
    assert result["ok"] is False
    assert result["completion_state"] == "blocked"
    assert result["incomplete_reason"] == "command_execution_failed"
    assert "command_execution_failed" in result["response"]
    assert "opencode_error" not in json.dumps(result)
    assert fake.send_message_called == 0
    assert fake.execute_command_called == 1

    skill_debug = result["_llm_debug"]["skill_invocation"]
    assert skill_debug["kind"] == "command"
    assert skill_debug["reason"] == "command_execution_failed"
    assert "command_execution_error" in skill_debug
    assert skill_debug["blocked"] is True

    event_types = {e.get("type") or e.get("event_type") for e in result["runtime_events"]}
    assert "skill.blocked" in event_types
    assert "skill.prompt_applied" not in event_types
    await client.close()

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


@pytest.mark.asyncio
async def test_chat_permission_pending_returns_blocked(tmp_path, monkeypatch):
    class PendingPermission(FakeOpenCodeClient):
        async def send_message(self, session_id, **kwargs):
            msg = {
                "id": "a-perm-1",
                "role": "assistant",
                "parts": [
                    {"type": "text", "text": "I need permission to fetch the Confluence page."},
                    {"type": "permission", "status": "pending", "id": "perm-1", "tool": "efp_confluence_get_page"},
                ],
            }
            self.messages[session_id] = [msg]
            return {"message": msg}
        async def list_messages(self, session_id): return list(self.messages[session_id])
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=PendingPermission()))); await client.start_server()
    payload = await (await client.post("/api/chat", json={"message": "q", "session_id": "s-perm-pending"})).json()
    assert payload["ok"] is False
    assert payload["completion_state"] == "blocked"
    assert payload["response"] != "I need permission to fetch the Confluence page."
    assert payload["_llm_debug"]["completion_probe"]["reason"] == "pending_permission"
    assert not any(e.get("type") == "complete" for e in payload["runtime_events"])
    await client.close()


@pytest.mark.asyncio
async def test_chat_permission_denied_not_success(tmp_path, monkeypatch):
    class DeniedPermission(FakeOpenCodeClient):
        async def send_message(self, session_id, **kwargs):
            msg = {
                "id": "a-perm-1",
                "role": "assistant",
                "parts": [
                    {"type": "text", "text": "Permission denied."},
                    {"type": "permission", "status": "denied", "id": "perm-1", "tool": "efp_confluence_get_page"},
                ],
            }
            self.messages[session_id] = [msg]
            return {"message": msg}
        async def list_messages(self, session_id): return list(self.messages[session_id])
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=DeniedPermission()))); await client.start_server()
    payload = await (await client.post("/api/chat", json={"message": "q", "session_id": "s-perm-denied"})).json()
    assert payload["ok"] is False
    assert payload["completion_state"] in {"blocked", "error"}
    assert "permission" in payload["response"].lower() or "denied" in payload["response"].lower()
    assert not any(e.get("type") == "complete" for e in payload["runtime_events"])
    await client.close()


@pytest.mark.asyncio
async def test_chat_permission_resolved_then_final_completed(tmp_path, monkeypatch):
    class PermissionResolved(FakeOpenCodeClient):
        def __init__(self): super().__init__(); self.calls = 0
        async def send_message(self, session_id, **kwargs):
            msg = {"id": "a-perm-1", "role": "assistant", "parts": [{"type": "text", "text": "Waiting for permission..."}, {"type": "permission", "status": "pending", "id": "perm-1", "tool": "efp_confluence_get_page"}]}
            self.messages[session_id] = [msg]
            return {"message": msg}
        async def list_messages(self, session_id):
            self.calls += 1
            if self.calls == 1:
                return list(self.messages[session_id])
            final = {"id": "a-final", "role": "assistant", "finish_reason": "stop", "parts": [{"type": "text", "text": "Agenda summary ..."}]}
            self.messages[session_id].append(final)
            return list(self.messages[session_id])
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_POLL_SECONDS", "0.01")
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=PermissionResolved()))); await client.start_server()
    payload = await (await client.post("/api/chat", json={"message": "q", "session_id": "s-perm-final"})).json()
    assert payload["ok"] is True
    assert payload["completion_state"] == "completed"
    assert payload["response"] == "Agenda summary ..."
    await client.close()

@pytest.mark.asyncio
async def test_chat_deleted_session_returns_410_and_no_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = FakeOpenCodeClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app)); await c.start_server()
    await c.post('/api/chat', json={'message':'hello','session_id':'s1'})
    app[SESSION_STORE_KEY].mark_deleted('s1')
    before = fake.create_calls
    r = await c.post('/api/chat', json={'message':'again','session_id':'s1'})
    body = await r.json()
    assert r.status == 410
    assert body['error'] == 'session_deleted'
    assert fake.create_calls == before
    assert app[SESSION_STORE_KEY].get('s1').deleted is True
    await c.close()


class _TrackSendDeletedClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__(); self.send_calls = 0
    async def send_message(self, *args, **kwargs):
        self.send_calls += 1
        return await super().send_message(*args, **kwargs)


@pytest.mark.asyncio
async def test_chat_deleted_session_does_not_send_or_create(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _TrackSendDeletedClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app)); await c.start_server()
    await c.post("/api/chat", json={"message":"hello","session_id":"s2"})
    app[SESSION_STORE_KEY].mark_deleted("s2")
    before_create = fake.create_calls
    before_send = fake.send_calls
    res = await c.post("/api/chat", json={"message":"again","session_id":"s2"})
    body = await res.json()
    assert res.status == 410 and body["error"] == "session_deleted"
    assert fake.create_calls == before_create
    assert fake.send_calls == before_send
    assert app[SESSION_STORE_KEY].get("s2").deleted is True
    await c.close()


@pytest.mark.asyncio
async def test_wait_for_completion_pending_then_completed_polls_until_completed():
    class C:
        def __init__(self): self.calls=0
        async def list_messages(self, _sid):
            self.calls += 1
            if self.calls == 1:
                return [{"id":"a1","role":"assistant","parts":[{"type":"text","text":"Creating files..."},{"type":"tool","status":"running"}]}]
            return [{"id":"a2","role":"assistant","finish_reason":"stop","parts":[{"type":"text","text":"done"}]}]
    probe, _ = await __import__('efp_opencode_adapter.chat_api', fromlist=['_wait_for_assistant_completion'])._wait_for_assistant_completion(client=C(), opencode_session_id='s', response_payload={}, before_messages=[], timeout_seconds=0.5, poll_seconds=0.01)
    assert probe["completion_state"] == "completed"


@pytest.mark.asyncio
async def test_wait_for_completion_pending_timeout_has_diagnostics():
    class C:
        async def list_messages(self, _sid):
            return [{"id":"a1","role":"assistant","parts":[{"type":"text","text":"Creating files..."},{"type":"tool","status":"running"}]}]
    probe, _ = await __import__('efp_opencode_adapter.chat_api', fromlist=['_wait_for_assistant_completion'])._wait_for_assistant_completion(client=C(), opencode_session_id='s', response_payload={}, before_messages=[], timeout_seconds=0.02, poll_seconds=0.01)
    assert probe["completion_state"] == "incomplete"
    assert probe["reason"] == "final_assistant_message_timeout"
    assert "timeout_seconds" in probe["diagnostics"] and "poll_seconds" in probe["diagnostics"] and "progress_preview" in probe["diagnostics"]


@pytest.mark.asyncio
async def test_chat_completed_with_empty_text_returns_empty_final(tmp_path, monkeypatch):
    class EmptyFinal(FakeOpenCodeClient):
        async def send_message(self, session_id, **kwargs):
            msg = {"id":"a1","role":"assistant","finish_reason":"stop","parts":[{"type":"text","text":""}]}
            self.messages[session_id]=[msg]
            return {"message": msg}
        async def list_messages(self, session_id):
            return list(self.messages[session_id])
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    c = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=EmptyFinal()))); await c.start_server()
    payload = await (await c.post("/api/chat", json={"message":"q","session_id":"s-empty-final"})).json()
    assert payload["ok"] is False and payload["completion_state"] == "empty_final"
    assert payload["incomplete_reason"] == "empty_final_assistant_text"
    assert "without a visible assistant response" in payload["response"]
    assert any(e.get("type") == "chat.empty_final" for e in payload["runtime_events"])
    chatlog = await (await c.get("/api/sessions/s-empty-final/chatlog")).json()
    assert chatlog["status"] != "success"
    await c.close()


class FragmentAssistantClient(FakeOpenCodeClient):
    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        text = parts[0].get("text", "")
        ses = self.messages[session_id]
        uid = f"u-{len(ses)+1}"
        ses.append({"info": {"id": uid, "role": "user"}, "parts": [{"type": "text", "text": text}]})
        ses.append({"info": {"id": "a-frag-1", "role": "assistant"}, "parts": [{"type": "text", "text": "part 1"}]})
        ses.append({"info": {"id": "a-frag-2", "role": "assistant"}, "parts": [{"type": "text", "text": "part 2"}]})
        return {"message": {"info": {"id": "a-frag-2", "role": "assistant"}, "parts": [{"type": "text", "text": "part 2"}]}}


@pytest.mark.asyncio
async def test_chat_api_returns_assistant_message_ids_for_fragments(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FragmentAssistantClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    resp = await client.post("/api/chat", json={"message": "hello", "session_id": "s-frag"})
    payload = await resp.json()
    assert resp.status == 200
    assert payload["assistant_message_ids"] == ["a-frag-1", "a-frag-2"]
    assert payload["assistant_message_id"] == "a-frag-2"
    assert payload["user_message_id"]
    assert payload["response"] == "part 2"
    assert payload["_llm_debug"]["message_ids"]["assistant_message_ids"] == ["a-frag-1", "a-frag-2"]
    await client.close()


class BeforeListFailClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self._first = True

    async def list_messages(self, session_id):
        if self._first:
            self._first = False
            raise RuntimeError("before failed")
        return await super().list_messages(session_id)


@pytest.mark.asyncio
async def test_chat_api_assistant_message_ids_fallback_when_before_list_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=BeforeListFailClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    resp = await client.post("/api/chat", json={"message": "hello", "session_id": "s-before-fail"})
    payload = await resp.json()
    assert resp.status == 200
    assert payload["assistant_message_id"]
    assert payload["assistant_message_ids"] == [payload["assistant_message_id"]]
    assert payload["_llm_debug"].get("message_id_detection_error_before")
    await client.close()


class BeforeFailHistoryClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self._before_phase = True

    async def list_messages(self, session_id):
        if self._before_phase:
            raise RuntimeError("before snapshot failed")
        return [
            {"info": {"id": "u-old", "role": "user"}},
            {"info": {"id": "a-old", "role": "assistant"}, "parts": [{"type": "text", "text": "old"}]},
            {"info": {"id": "u-new", "role": "user"}},
            {"info": {"id": "a-new", "role": "assistant"}, "parts": [{"type": "text", "text": "new"}]},
        ]

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        self._before_phase = False
        return {"message": {"info": {"id": "a-new", "role": "assistant"}, "parts": [{"type": "text", "text": "new"}]}}


@pytest.mark.asyncio
async def test_chat_response_assistant_message_ids_do_not_include_history_when_before_snapshot_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=BeforeFailHistoryClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    payload = await (await client.post("/api/chat", json={"message": "hello", "session_id": "s-before-history"})).json()
    assert payload["ok"] is True
    assert payload["completion_state"] == "completed"
    assert payload["response"] == "new" or "new" in payload["response"]
    assert payload["assistant_message_ids"] == ["a-new"]
    assert payload["assistant_message_id"] == "a-new"
    assert "a-old" not in payload["assistant_message_ids"]
    assert payload["_llm_debug"]["message_ids"]["assistant_message_ids"] == ["a-new"]
    assert payload["_llm_debug"]["message_ids"]["assistant_message_id"] == "a-new"
    assert payload["_llm_debug"].get("message_id_detection_error_before")
    await client.close()


class BeforeFailNoCurrentCompletionClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self._calls = 0

    async def list_messages(self, session_id):
        self._calls += 1
        if self._calls == 1:
            raise RuntimeError("before snapshot failed")
        return [
            {"info": {"id": "u-old", "role": "user"}},
            {"info": {"id": "a-old", "role": "assistant"}, "parts": [{"type": "text", "text": "old"}]},
        ]

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        return {"message": {"info": {"id": "u-new", "role": "user"}, "parts": [{"type": "text", "text": "new user"}]}}


@pytest.mark.asyncio
async def test_chat_before_snapshot_unreliable_without_current_completion_does_not_use_history_completed_message(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=BeforeFailNoCurrentCompletionClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    payload = await (await client.post("/api/chat", json={"message": "hello", "session_id": "s-before-no-current"})).json()
    assert payload["ok"] is False
    assert payload["completion_state"] in {"incomplete", "empty_final", "error", "failed"}
    assert "a-old" not in payload["assistant_message_ids"]
    await client.close()


class AfterFailClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self._calls = 0

    async def list_messages(self, session_id):
        self._calls += 1
        if self._calls >= 2:
            raise RuntimeError("after snapshot failed")
        return await super().list_messages(session_id)

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        return {"message": {"info": {"id": "a-after-fallback", "role": "assistant"}, "parts": [{"type": "text", "text": "ok"}]}}


@pytest.mark.asyncio
async def test_chat_response_assistant_message_ids_fallback_when_after_snapshot_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=AfterFailClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    payload = await (await client.post("/api/chat", json={"message": "hello", "session_id": "s-after-fail"})).json()
    assert payload["ok"] is True
    assert payload["assistant_message_ids"] == [payload["assistant_message_id"]]
    assert payload["_llm_debug"].get("message_id_detection_error_after")
    await client.close()


@pytest.mark.asyncio
async def test_chat_response_blocked_without_known_assistant_id_returns_empty_assistant_message_ids(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "skills-index.json").write_text(
        json.dumps({"skills": [{"efp_name": "demo_skill", "opencode_name": "demo-skill", "opencode_supported": True, "runtime_equivalence": True, "programmatic": False, "missing_tools": [], "missing_opencode_tools": []}]}),
        encoding="utf-8",
    )
    cfg = tmp_path / "opencode.json"
    cfg.write_text(json.dumps({"permission": {"skill": {"*": "deny"}}}), encoding="utf-8")
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(cfg))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    payload = await (await client.post("/api/chat", json={"message": "/demo-skill hi", "session_id": "s-blocked-empty"})).json()
    assert payload["ok"] is False
    assert payload["completion_state"] in ("blocked", "empty_final", "error", "failed", "incomplete")
    assert payload["assistant_message_id"] == ""
    assert payload["assistant_message_ids"] == []
    await client.close()
