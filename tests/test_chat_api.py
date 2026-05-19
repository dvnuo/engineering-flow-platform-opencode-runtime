import json
import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.opencode_client import OpenCodeClientError, OpenCodeTransportDisconnected, OpenCodeTransportTimeout
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.app_keys import ATTACHMENT_SERVICE_KEY, CHAT_RUN_STORE_KEY, EVENT_BUS_KEY, SESSION_STORE_KEY, REQUEST_BINDING_STORE_KEY, USER_DISPLAY_STORE_KEY
from efp_opencode_adapter import chat_api
from efp_opencode_adapter.chat_api import _consume_background_chat_task, _is_stream_relevant_event
from efp_opencode_adapter.skill_invocation import SkillDecision
from test_t06_helpers import FakeOpenCodeClient


class _UserOnlyIncompleteClient(FakeOpenCodeClient):
    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        user_text = parts[0].get("text", "")
        self.messages[session_id].append({"id": message_id or "u-incomplete", "role": "user", "parts": [{"type": "text", "text": user_text}]})
        return {"message": {"id": message_id or "u-incomplete", "role": "user", "parts": [{"type": "text", "text": user_text}]}}


class _FailingChatClient(FakeOpenCodeClient):
    async def send_message(self, *args, **kwargs):
        raise OpenCodeClientError("upstream boom token=ghp_secret", status=500)


class _SlowChatClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        self.entered.set()
        await self.release.wait()
        return await super().send_message(session_id, parts=parts, model=model, agent=agent, system=system, message_id=message_id, no_reply=no_reply, tools=tools)


class _SubmitTimeoutStillRunningClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.user_message_id = "u-timeout"

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        self.user_message_id = message_id or "u-timeout"
        self.messages[session_id].append({"id": self.user_message_id, "role": "user", "parts": parts})
        raise OpenCodeTransportTimeout("POST", f"/session/{session_id}/message", 300, asyncio.TimeoutError())

    async def get_session_status(self, timeout_seconds=30):
        return {"sessions": {"ses-1": {"state": "running"}}}


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
async def test_handle_chat_payload_creates_and_completes_chat_run(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/api/chat", json={"message": "hello", "session_id": "s-run", "request_id": "r-run"})
        body = await resp.json()

        assert resp.status == 200
        assert body["completion_state"] == "completed"
        run = app[CHAT_RUN_STORE_KEY].get("r-run")
        assert run.status == "completed"
        assert run.completion_state == "completed"
        assert run.user_message_id == body["user_message_id"]
        assert run.assistant_message_id == body["assistant_message_id"]

        run_resp = await client.get("/api/chat/runs/r-run")
        run_body = await run_resp.json()
        assert run_body["success"] is True
        assert run_body["run"]["final_payload"]["response"] == "echo: hello"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_handle_chat_payload_marks_incomplete_and_failed_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state-incomplete"))
    monkeypatch.setenv("EFP_CHAT_AUTO_CONTINUE_ENABLED", "false")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_POLL_SECONDS", "0.01")
    app = create_app(Settings.from_env(), opencode_client=_UserOnlyIncompleteClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/api/chat", json={"message": "no final", "session_id": "s-incomplete", "request_id": "r-incomplete"})
        body = await resp.json()
        assert resp.status == 200
        assert body["completion_state"] == "incomplete"
        assert app[CHAT_RUN_STORE_KEY].get("r-incomplete").status == "incomplete"
    finally:
        await client.close()

    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state-failed"))
    app = create_app(Settings.from_env(), opencode_client=_FailingChatClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/api/chat", json={"message": "fail", "session_id": "s-failed", "request_id": "r-failed"})
        body = await resp.json()
        assert resp.status == 502
        assert body["error"] == "opencode_error"
        run = app[CHAT_RUN_STORE_KEY].get("r-failed")
        assert run.status == "failed"
        assert run.completion_state == "error"
        assert "ghp_secret" not in json.dumps(run.final_payload)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_active_chat_run_prevents_new_chat_same_session(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = _SlowChatClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        first = asyncio.create_task(client.post("/api/chat", json={"message": "first", "session_id": "s-active", "request_id": "r-active-1"}))
        await fake.entered.wait()
        second = await client.post("/api/chat", json={"message": "second", "session_id": "s-active", "request_id": "r-active-2"})
        body = await second.json()
        assert second.status == 409
        assert body["error"] == "chat_run_already_active"
        assert body["active_run"]["request_id"] == "r-active-1"
        fake.release.set()
        await first
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_submit_timeout_recovery_still_running_keeps_run_active_not_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_TIMEOUT_RECOVERY_MAX_SECONDS", "0.01")
    monkeypatch.setenv("EFP_CHAT_TIMEOUT_RECOVERY_POLL_SECONDS", "0.001")
    monkeypatch.setenv("EFP_CHAT_TOTAL_WALL_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "0.01")
    app = create_app(Settings.from_env(), opencode_client=_SubmitTimeoutStillRunningClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/api/chat", json={"message": "slow", "session_id": "s-timeout", "request_id": "r-timeout"})
        body = await resp.json()

        assert resp.status == 200
        assert body["completion_state"] == "incomplete"
        run = app[CHAT_RUN_STORE_KEY].get("r-timeout")
        assert run.status in {"running", "stream_detached"}
        assert run.status != "failed"
        assert run.metadata["opencode_may_still_be_running"] is True
        active = app[CHAT_RUN_STORE_KEY].active_for_session("s-timeout")
        assert active.request_id == "r-timeout"
    finally:
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
async def test_chat_stream_error_emits_final_before_done(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    async def _raise_bad_gateway(request, payload):
        raise OpenCodeClientError("upstream_failed", payload="sensitive token=abc")

    monkeypatch.setattr(chat_api, "handle_chat_payload", _raise_bad_gateway)
    try:
        resp = await client.post("/api/chat/stream", json={"message": "boom", "session_id": "s-stream-err", "request_id": "r-stream-err"})
        body = await resp.text()
        assert "event: error" in body
        assert "event: final" in body
        assert "event: done" in body
        assert body.index("event: final") < body.index("event: done")

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
        assert final_data["ok"] is False
        assert final_data["completion_state"] == "error"
        assert final_data["incomplete_reason"]
        assert final_data["response"]
        assert final_data["session_id"] == "s-stream-err"
        assert final_data["request_id"] == "r-stream-err"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_chat_stream_never_emits_empty_final_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    async def _return_none(request, payload):
        return None

    monkeypatch.setattr(chat_api, "handle_chat_payload", _return_none)
    try:
        resp = await client.post("/api/chat/stream", json={"message": "none-final", "session_id": "s-none", "request_id": "r-none"})
        body = await resp.text()
        assert "event: final\ndata: {}\n\n" not in body

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
        assert final_data["completion_state"]
        assert final_data["response"]
        assert final_data["request_id"] == "r-none"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stream_error_response_emits_final_and_done_for_invalid_json(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/api/chat/stream", data="{", headers={"Content-Type": "application/json"})
        body = await resp.text()
        assert "event: error" in body
        assert "event: final" in body
        assert "event: done" in body
        assert body.index("event: final") < body.index("event: done")
        events = []
        for chunk in body.strip().split("\n\n"):
            event_name = None
            data_line = None
            for line in chunk.splitlines():
                if line.startswith("event: "):
                    event_name = line.removeprefix("event: ")
                elif line.startswith("data: "):
                    data_line = line.removeprefix("data: ")
            if event_name and data_line:
                events.append((event_name, json.loads(data_line)))
        final_data = next(payload for name, payload in events if name == "final")
        assert final_data["ok"] is False
        assert final_data["completion_state"] == "error"
        assert final_data["incomplete_reason"]
        assert final_data["response"]
        assert final_data["runtime_events"] == []
        assert final_data["events"] == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stream_error_response_emits_final_for_non_object_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/api/chat/stream", json=[])
        body = await resp.text()
        assert "event: error" in body
        assert "event: final" in body
        assert "event: done" in body
        assert body.index("event: final") < body.index("event: done")
        events = []
        for chunk in body.strip().split("\n\n"):
            event_name = None
            data_line = None
            for line in chunk.splitlines():
                if line.startswith("event: "):
                    event_name = line.removeprefix("event: ")
                elif line.startswith("data: "):
                    data_line = line.removeprefix("data: ")
            if event_name and data_line:
                events.append((event_name, json.loads(data_line)))
        final_data = next(payload for name, payload in events if name == "final")
        assert final_data["ok"] is False
        assert final_data["completion_state"] == "error"
        assert final_data["incomplete_reason"]
        assert final_data["response"]
        assert final_data["runtime_events"] == []
        assert final_data["events"] == []
    finally:
        await client.close()


def test_stream_error_final_payload_uses_chat_auto_continue_setting():
    class _SettingsStub:
        chat_auto_continue_enabled = True

    settings = _SettingsStub()
    payload = chat_api._stream_error_final_payload(
        error_payload={"error": "chat_failed", "detail": "failed detail"},
        session_id="s1",
        request_id="r1",
        runtime_events=[],
        settings=settings,
    )
    assert payload["auto_continue_enabled"] is True


def test_stream_error_final_payload_debug_is_previewed_not_raw():
    long_detail = "token=secret-secret-secret " + ("very-long-fragment-" * 200)
    payload = chat_api._stream_error_final_payload(
        error_payload={"error": "opencode_error", "detail": long_detail},
        session_id="s1",
        request_id="r1",
        runtime_events=[],
        settings=None,
    )
    debug_detail = payload["_llm_debug"]["stream_error"]["detail"]
    assert len(debug_detail) <= 520
    assert debug_detail != long_detail
    assert ("very-long-fragment-" * 50) not in debug_detail


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
async def test_chat_persists_original_display_for_slash_skill_with_csv_attachment(tmp_path, monkeypatch):
    class CaptureSkillClient(FakeOpenCodeClient):
        def __init__(self):
            super().__init__()
            self.sent_parts = []

        async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
            self.sent_parts.append(parts)
            user_id = message_id or f"u-{len(self.messages[session_id]) + 1}"
            user = {"id": user_id, "role": "user", "parts": parts}
            assistant = {
                "id": f"a-{len(self.messages[session_id]) + 2}",
                "role": "assistant",
                "parts": [{"type": "text", "text": "skill done"}, {"type": "step-finish", "reason": "stop"}],
            }
            self.messages[session_id].extend([user, assistant])
            return {"message": assistant, "usage": {"input_tokens": 10, "output_tokens": 5, "cost": 0.001}, "model": model or "test-model", "provider": "test-provider"}

    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    fake = CaptureSkillClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    upload = app[ATTACHMENT_SERVICE_KEY].upload("webchat_test", "cases.csv", b"summary,steps\nA,B", "text/csv")

    def _allow_skill(settings, invocation):
        return SkillDecision(
            skill={"opencode_name": "jira-bulk-create-from-csv"},
            allowed=True,
            reason="allowed",
            permission_state="allow",
        )

    monkeypatch.setattr(chat_api, "evaluate_skill_invocation", _allow_skill)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post(
            "/api/chat",
            json={
                "message": "/jira-bulk-create-from-csv example: https://jira.company.com/browse/MMGFX-13887",
                "session_id": "webchat_test",
                "request_id": "req_test",
                "attachments": [
                    {
                        **upload,
                        "url": "https://files.invalid/cases.csv",
                        "text": "summary,steps\nA,B",
                        "content": "raw csv content",
                    }
                ],
            },
        )
        body = await resp.json()

        assert resp.status == 200
        assert fake.sent_parts
        sent_parts = fake.sent_parts[-1]
        assert sent_parts[0]["synthetic"] is True
        assert sent_parts[0]["metadata"]["efp_internal"] == "skill_prompt"
        assert "Run the OpenCode agent skill `jira-bulk-create-from-csv`" in sent_parts[0]["text"]
        assert any(
            part.get("type") == "text"
            and "Attached files:" in part.get("text", "")
            and "summary,steps" in part.get("text", "")
            and part.get("synthetic") is True
            and part.get("metadata", {}).get("efp_internal") == "attachment_context"
            for part in sent_parts
        )

        record = app[USER_DISPLAY_STORE_KEY].get_user_message(
            app[SESSION_STORE_KEY].get("webchat_test").opencode_session_id,
            body["user_message_id"],
            "webchat_test",
        )
        assert record["display_content"] == "/jira-bulk-create-from-csv example: https://jira.company.com/browse/MMGFX-13887"
        assert record["display_attachments"] == [
            {
                "file_id": upload["file_id"],
                "name": "cases.csv",
                "content_type": "text/csv",
                "size": len(b"summary,steps\nA,B"),
                "type": "file",
            }
        ]
        serialized = json.dumps(record)
        assert "summary,steps" not in serialized
        assert "https://files.invalid/cases.csv" not in serialized
        assert "raw csv content" not in serialized
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_known_skill_native_command_api_receives_display_message_id(tmp_path, monkeypatch):
    class KnownCommandClient(FakeOpenCodeClient):
        def __init__(self):
            super().__init__()
            self.execute_command_called = False
            self.execute_command_message_id = ""

        async def list_commands(self, timeout_seconds=30):
            return [{"name": "some-skill"}]

        async def execute_command(self, session_id, *, command, arguments, model, agent, message_id=None):
            self.execute_command_called = True
            self.execute_command_message_id = message_id or ""
            assistant = {
                "id": "a-known-command",
                "role": "assistant",
                "finish_reason": "stop",
                "parts": [{"type": "text", "text": "known command ok"}],
            }
            self.messages[session_id].extend([
                {"id": message_id, "role": "user", "parts": [{"type": "text", "text": "/some-skill arg1"}]},
                assistant,
            ])
            return {"message": assistant}

        async def send_message(self, *args, **kwargs):
            raise AssertionError("send_message should not be called for no-attachment native commands")

    def _allow_skill(settings, invocation):
        return SkillDecision(
            skill={"opencode_name": "some-skill"},
            allowed=True,
            reason="allowed",
            permission_state="allow",
        )

    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(chat_api, "evaluate_skill_invocation", _allow_skill)
    fake = KnownCommandClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            "/api/chat",
            json={
                "message": "/some-skill arg1",
                "session_id": "webchat_command_id",
                "request_id": "req_command_id",
                "attachments": [],
            },
        )
        payload = await response.json()

        assert response.status == 200
        assert fake.execute_command_called is True
        assert fake.execute_command_message_id == payload["user_message_id"]
        assert fake.execute_command_message_id.startswith("msg_")
        record = app[USER_DISPLAY_STORE_KEY].get_user_message(
            app[SESSION_STORE_KEY].get("webchat_command_id").opencode_session_id,
            payload["user_message_id"],
            "webchat_command_id",
        )
        assert record["display_content"] == "/some-skill arg1"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unknown_native_command_api_receives_display_message_id(tmp_path, monkeypatch):
    class UnknownCommandClient(FakeOpenCodeClient):
        def __init__(self):
            super().__init__()
            self.execute_command_message_id = ""

        async def list_commands(self, timeout_seconds=30):
            return [{"name": "native-command"}]

        async def execute_command(self, session_id, *, command, arguments, model, agent, message_id=None):
            self.execute_command_message_id = message_id or ""
            assistant = {
                "id": "a-native-command",
                "role": "assistant",
                "finish_reason": "stop",
                "parts": [{"type": "text", "text": "native command ok"}],
            }
            self.messages[session_id].extend([
                {"id": message_id, "role": "user", "parts": [{"type": "text", "text": "/native-command abc"}]},
                assistant,
            ])
            return {"message": assistant}

        async def send_message(self, *args, **kwargs):
            raise AssertionError("send_message should not be called for no-attachment native commands")

    def _unknown_skill(settings, invocation):
        return SkillDecision(skill=None, allowed=False, reason="unknown_skill", permission_state="unknown")

    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(chat_api, "evaluate_skill_invocation", _unknown_skill)
    fake = UnknownCommandClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            "/api/chat",
            json={
                "message": "/native-command abc",
                "session_id": "webchat_unknown_command_id",
                "request_id": "req_unknown_command_id",
                "attachments": [],
            },
        )
        payload = await response.json()
        history = await (await client.get("/api/sessions/webchat_unknown_command_id")).json()

        assert response.status == 200
        assert fake.execute_command_message_id == payload["user_message_id"]
        assert fake.execute_command_message_id.startswith("msg_")
        record = app[USER_DISPLAY_STORE_KEY].get_user_message(
            app[SESSION_STORE_KEY].get("webchat_unknown_command_id").opencode_session_id,
            payload["user_message_id"],
            "webchat_unknown_command_id",
        )
        assert record["display_content"] == "/native-command abc"
        user_messages = [message for message in history["messages"] if message["role"] == "user"]
        assert user_messages[0]["id"] == payload["user_message_id"]
        assert user_messages[0]["content"] == "/native-command abc"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unknown_native_command_with_csv_attachment_uses_prompt_fallback(tmp_path, monkeypatch):
    class UnknownCommandWithAttachmentClient(FakeOpenCodeClient):
        def __init__(self):
            super().__init__()
            self.execute_command_called = False
            self.send_message_called = False
            self.send_message_message_id = ""
            self.sent_parts = []

        async def list_commands(self, timeout_seconds=30):
            return [{"name": "native-command"}]

        async def execute_command(self, *args, **kwargs):
            self.execute_command_called = True
            raise AssertionError("command API must not be used when attachments are present")

        async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
            self.send_message_called = True
            self.send_message_message_id = message_id or ""
            self.sent_parts = parts
            assistant = {
                "id": "a-native-fallback",
                "role": "assistant",
                "finish_reason": "stop",
                "parts": [{"type": "text", "text": "fallback ok"}],
            }
            self.messages[session_id].extend([
                {"id": message_id, "role": "user", "parts": parts},
                assistant,
            ])
            return {"message": assistant}

    def _unknown_skill(settings, invocation):
        return SkillDecision(skill=None, allowed=False, reason="unknown_skill", permission_state="unknown")

    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setattr(chat_api, "evaluate_skill_invocation", _unknown_skill)
    fake = UnknownCommandWithAttachmentClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    upload = app[ATTACHMENT_SERVICE_KEY].upload("webchat_unknown_attachment", "cases.csv", b"summary,steps\nA,B", "text/csv")
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            "/api/chat",
            json={
                "message": "/native-command abc",
                "session_id": "webchat_unknown_attachment",
                "request_id": "req_unknown_attachment",
                "attachments": [
                    {
                        **upload,
                        "url": "https://files.invalid/cases.csv",
                        "text": "summary,steps\nA,B",
                        "raw": "raw csv content",
                        "base64": "c3VtbWFyeSxzdGVwcw==",
                    }
                ],
            },
        )
        payload = await response.json()

        assert response.status == 200
        assert fake.execute_command_called is False
        assert fake.send_message_called is True
        assert fake.send_message_message_id == payload["user_message_id"]
        assert any(
            part.get("type") == "text"
            and part.get("synthetic") is True
            and part.get("metadata", {}).get("efp_internal") == "attachment_context"
            and "summary,steps" in part.get("text", "")
            for part in fake.sent_parts
        )
        record = app[USER_DISPLAY_STORE_KEY].get_user_message(
            app[SESSION_STORE_KEY].get("webchat_unknown_attachment").opencode_session_id,
            payload["user_message_id"],
            "webchat_unknown_attachment",
        )
        assert record["display_content"] == "/native-command abc"
        serialized = json.dumps(record)
        for forbidden in ["summary,steps", "https://files.invalid/cases.csv", "raw csv content", "c3VtbWFyeSxzdGVwcw=="]:
            assert forbidden not in serialized
    finally:
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
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "0.2")
    c = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=EmptyFinal()))); await c.start_server()
    payload = await (await c.post("/api/chat", json={"message":"q","session_id":"s-empty-final"})).json()
    assert payload["ok"] is False and payload["completion_state"] in {"empty_final", "incomplete"}
    assert payload["incomplete_reason"] in {"empty_final_assistant_text", "final_assistant_message_timeout"}
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
    resp = await client.post("/api/chat", json={"message": "hello", "session_id": "s-before-history"})
    payload = await resp.json()
    assert resp.status == 200
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
async def test_before_snapshot_unreliable_without_response_completion_does_not_trust_after_history(tmp_path, monkeypatch):
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

@pytest.mark.asyncio
async def test_attachment_debug_redaction_does_not_touch_binding_store():
    from efp_opencode_adapter.chat_api import _redact_attachment_payloads_for_debug
    out = _redact_attachment_payloads_for_debug({"url": "data:text/plain;base64,AAAA"})
    assert out["url"].startswith("data:")


class AutoContinueClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.calls = 0
        self.sent_ids = []

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        self.calls += 1
        if message_id:
            self.sent_ids.append(message_id)
        if self.calls == 1:
            return {"message": {"info": {"id": "a1", "role": "assistant"}, "parts": [{"type": "text", "text": "I am reading the repository..."}]}}
        return {"message": {"info": {"id": "a2", "role": "assistant"}, "parts": [{"type": "text", "text": "final answer"}]}}


@pytest.mark.asyncio
async def test_chat_auto_continue_progress_then_final(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_POLL_SECONDS", "0.01")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_POLL_SECONDS", "0.01")
    c = AutoContinueClient()
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=c))); await client.start_server()
    payload = await (await client.post('/api/chat', json={"message":"q","session_id":"s1","request_id":"r1"})).json()
    assert payload["completion_state"] == "completed"
    assert payload["continuation_count"] == 1
    assert payload["response"] == "final answer"
    assert not payload["incomplete_reason"]
    continuation_metadata = payload["metadata"]["continuation"]
    assert continuation_metadata["enabled"] is True
    assert continuation_metadata["turns_attempted"] == 1
    assert continuation_metadata["max_turns"] >= 1
    assert continuation_metadata["debug"][0]["message_id"].startswith("msg")
    assert "signature_before" in continuation_metadata["debug"][0]
    assert "signature_after" in continuation_metadata["debug"][0]
    types = [e.get("type") for e in payload["runtime_events"]]
    assert "continuation.started" in types and "continuation.completed" in types
    continuation_event = next(e for e in payload["runtime_events"] if e.get("type") == "continuation.completed")
    for key in ("event_type", "request_id", "session_id", "turn_index", "message_id", "reason", "state", "created_at", "metadata"):
        assert key in continuation_event
    await client.close()


class AutoContinueFinalMessageFromListClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        self.calls += 1
        if self.calls == 1:
            self.messages[session_id].append(
                {
                    "id": "assistant-progress-1",
                    "role": "assistant",
                    "parts": [{"type": "text", "text": "I am reading the repository..."}],
                }
            )
            return {
                "message": {
                    "info": {"id": "assistant-progress-1", "role": "assistant"},
                    "parts": [{"type": "text", "text": "I am reading the repository..."}],
                }
            }
        self.messages[session_id].append(
            {
                "id": "assistant-final-2",
                "role": "assistant",
                "parts": [{"type": "text", "text": "Done. Summary..."}],
            }
        )
        return {}


@pytest.mark.asyncio
async def test_chat_auto_continue_binds_final_assistant_message_id(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_POLL_SECONDS", "0.01")
    c = AutoContinueFinalMessageFromListClient()
    app = create_app(Settings.from_env(), opencode_client=c)
    monkeypatch.setattr(app[REQUEST_BINDING_STORE_KEY], "complete", lambda request_id: None)
    client = TestClient(TestServer(app))
    await client.start_server()
    payload = await (await client.post("/api/chat", json={"message": "q", "session_id": "s-final-bind", "request_id": "r-final-bind"})).json()
    assert payload["ok"] is True
    assert payload["completion_state"] == "completed"
    assert payload["continuation_count"] == 1
    assert payload["response"] == "Done. Summary..."
    assert payload["assistant_message_id"] == "assistant-final-2" or "assistant-final-2" in payload["assistant_message_ids"]
    binding_store = app[REQUEST_BINDING_STORE_KEY]
    record = app[SESSION_STORE_KEY].get("s-final-bind")
    final_binding = binding_store.resolve(record.opencode_session_id, message_id="assistant-final-2")
    assert final_binding is not None and final_binding.request_id == "r-final-bind"
    await client.close()


@pytest.mark.asyncio
async def test_initial_message_ids_are_bound(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_POLL_SECONDS", "0.01")
    c = AutoContinueClient()
    app = create_app(Settings.from_env(), opencode_client=c)
    monkeypatch.setattr(app[REQUEST_BINDING_STORE_KEY], "complete", lambda request_id: None)
    client = TestClient(TestServer(app)); await client.start_server()
    await (await client.post('/api/chat', json={"message":"q","session_id":"s1","request_id":"rbind"})).json()
    binding_store = app[REQUEST_BINDING_STORE_KEY]
    record = app[SESSION_STORE_KEY].get("s1")
    generated_id = next(mid for mid in c.sent_ids if isinstance(mid, str) and mid.startswith("msg"))
    resolved = binding_store.resolve(record.opencode_session_id, message_id=generated_id)
    assert resolved is not None and resolved.request_id == "rbind"
    assert generated_id.startswith("msg")
    await client.close()


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ({"type": "tool.started", "session_id": "s1", "request_id": "r1"}, True),
        ({"type": "tool.started", "session_id": "s1", "data": {"request_id": "r1"}}, True),
        ({"type": "tool.started", "session_id": "s1", "portal_request_id": "r1", "request_id": "old"}, True),
        ({"type": "tool.started", "session_id": "s1", "request_id": "old"}, False),
        ({"type": "tool.started", "session_id": "s1", "data": {"request_id": "old"}}, False),
        ({"type": "message.delta", "session_id": "s1", "data": {"delta": "hello"}}, False),
        ({"type": "tool.started", "session_id": "other", "request_id": "r1"}, False),
        ({"type": "stream.started", "session_id": "s1"}, True),
    ],
)
def test_chat_stream_filters_events_by_portal_request(event, expected):
    assert _is_stream_relevant_event(event, session_id="s1", request_id="r1") is expected


def test_chat_stream_filter_ignores_raw_opencode_request_ids():
    event = {
        "type": "tool.started",
        "session_id": "s1",
        "raw_request_id": "r1",
        "opencode_request_id": "r1",
        "data": {"raw_request_id": "r1", "opencode_request_id": "r1"},
    }
    assert _is_stream_relevant_event(event, session_id="s1", request_id="r1") is False


class EmptyTimeoutNoProgressClient(FakeOpenCodeClient):
    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        return {}

    async def list_messages(self, session_id):
        return []


@pytest.mark.asyncio
async def test_chat_auto_continue_empty_timeout_no_progress_stops(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_POLL_SECONDS", "0.01")
    monkeypatch.setenv("EFP_CHAT_AUTO_CONTINUE_MAX_TURNS", "3")
    monkeypatch.setenv("EFP_CHAT_NO_PROGRESS_TIMEOUT_SECONDS", "0.001")
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=EmptyTimeoutNoProgressClient())))
    await client.start_server()
    payload = await (await client.post("/api/chat", json={"message": "q", "session_id": "s-empty-timeout"})).json()
    assert payload["ok"] is False
    assert payload["completion_state"] == "incomplete"
    assert "no_progress_timeout" in payload["incomplete_reason"]
    assert payload["continuation_count"] == 1
    no_progress_event = next(e for e in payload["runtime_events"] if e.get("type") == "continuation.no_progress")
    assert no_progress_event["state"] == "incomplete"
    assert no_progress_event["metadata"]["no_progress_timeout_seconds"] == 0.001
    assert "signature_after" in no_progress_event["metadata"]
    await client.close()


class EmptyTimeoutThenFinalClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        self.calls += 1
        if self.calls == 1:
            return {}
        return {"message": {"info": {"id": "a-final", "role": "assistant"}, "parts": [{"type": "text", "text": "Done. Summary..."}]}}

    async def list_messages(self, session_id):
        return []


class RuntimeProgressNoFinalClient(FakeOpenCodeClient):
    def __init__(self, *, session_id: str, request_id: str):
        super().__init__()
        self.bus = None
        self.portal_session_id = session_id
        self.portal_request_id = request_id
        self.progress_events = 0

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        return {}

    async def list_messages(self, session_id):
        if self.bus is not None:
            self.progress_events += 1
            await self.bus.publish(
                {
                    "type": "message.delta",
                    "session_id": self.portal_session_id,
                    "request_id": self.portal_request_id,
                    "raw_type": "message.part.delta",
                    "data": {"delta": f"working {self.progress_events}"},
                }
            )
        return []


@pytest.mark.asyncio
async def test_chat_auto_continue_runtime_events_count_as_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "0.005")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_POLL_SECONDS", "0.001")
    monkeypatch.setenv("EFP_CHAT_AUTO_CONTINUE_MAX_TURNS", "2")
    monkeypatch.setenv("EFP_CHAT_NO_PROGRESS_TIMEOUT_SECONDS", "0.05")
    fake = RuntimeProgressNoFinalClient(session_id="s-runtime-progress", request_id="r-runtime-progress")
    app = create_app(Settings.from_env(), opencode_client=fake)
    fake.bus = app[EVENT_BUS_KEY]
    client = TestClient(TestServer(app))
    await client.start_server()
    payload = await (await client.post("/api/chat", json={"message": "q", "session_id": "s-runtime-progress", "request_id": "r-runtime-progress"})).json()
    assert payload["ok"] is False
    assert payload["completion_state"] == "incomplete"
    assert payload["incomplete_reason"] == "auto_continue_max_turns_reached"
    assert payload["continuation_count"] == 2
    assert not any(e.get("type") == "continuation.no_progress" for e in payload["runtime_events"])
    assert fake.progress_events > 0
    assert payload["metadata"]["continuation"]["debug"][-1]["last_progress_event_type"] == "message.delta"
    await client.close()


class ForeignProgressNoFinalClient(FakeOpenCodeClient):
    def __init__(self, *, session_id: str, events: list[dict]):
        super().__init__()
        self.bus = None
        self.portal_session_id = session_id
        self.events = events

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        return {}

    async def list_messages(self, session_id):
        if self.bus is not None:
            for event in self.events:
                await self.bus.publish({**event, "session_id": self.portal_session_id})
        return []


@pytest.mark.asyncio
async def test_progress_events_from_other_request_do_not_count_as_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "0.005")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_POLL_SECONDS", "0.001")
    monkeypatch.setenv("EFP_CHAT_AUTO_CONTINUE_MAX_TURNS", "2")
    monkeypatch.setenv("EFP_CHAT_NO_PROGRESS_TIMEOUT_SECONDS", "0.01")
    fake = ForeignProgressNoFinalClient(
        session_id="s-foreign-progress",
        events=[
            {"type": "message.delta", "request_id": "req-other", "raw_type": "message.part.delta", "data": {"delta": "other"}},
            {"type": "tool.completed", "request_id": "req-other", "raw_type": "tool.complete"},
        ],
    )
    app = create_app(Settings.from_env(), opencode_client=fake)
    fake.bus = app[EVENT_BUS_KEY]
    client = TestClient(TestServer(app))
    await client.start_server()
    payload = await (await client.post("/api/chat", json={"message": "q", "session_id": "s-foreign-progress", "request_id": "req-current"})).json()
    assert payload["completion_state"] == "incomplete"
    assert "no_progress_timeout" in payload["incomplete_reason"]
    assert payload["metadata"]["continuation"]["debug"][-1]["last_progress_event_type"] != "message.delta"
    assert not any(e.get("request_id") == "req-other" for e in payload["runtime_events"])
    await client.close()


@pytest.mark.asyncio
async def test_session_level_bridge_events_without_request_do_not_prevent_no_progress_for_request_specific_work(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "0.005")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_POLL_SECONDS", "0.001")
    monkeypatch.setenv("EFP_CHAT_AUTO_CONTINUE_MAX_TURNS", "2")
    monkeypatch.setenv("EFP_CHAT_NO_PROGRESS_TIMEOUT_SECONDS", "0.01")
    fake = ForeignProgressNoFinalClient(
        session_id="s-bridge-status",
        events=[{"type": "event_bridge.connected", "engine": "opencode", "data": {"state": "connected"}}],
    )
    app = create_app(Settings.from_env(), opencode_client=fake)
    fake.bus = app[EVENT_BUS_KEY]
    client = TestClient(TestServer(app))
    await client.start_server()
    payload = await (await client.post("/api/chat", json={"message": "q", "session_id": "s-bridge-status", "request_id": "req-bridge-status"})).json()
    assert payload["completion_state"] == "incomplete"
    assert "no_progress_timeout" in payload["incomplete_reason"]
    assert payload["metadata"]["continuation"]["debug"][-1]["last_progress_event_type"] != "event_bridge.connected"
    await client.close()


class SubmitTimeoutRecoveredClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.timed_out = False
        self.pending_user_id = ""

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        self.timed_out = True
        self.pending_user_id = message_id or "u-timeout"
        raise OpenCodeTransportTimeout("POST", f"/session/{session_id}/message", 300, asyncio.TimeoutError())

    async def get_session_status(self, timeout_seconds=30):
        return {"sessions": {"ses-1": {"state": "idle"}}}

    async def list_messages(self, session_id):
        if self.timed_out:
            return [
                {"id": self.pending_user_id, "role": "user", "parts": [{"type": "text", "text": "q"}]},
                {"id": "a-recovered", "role": "assistant", "parts": [{"type": "text", "text": "Recovered answer"}]},
            ]
        return []


class SubmitTransportDisconnectedRecoveredClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.calls = 0
        self.pending_user_id = ""

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        self.calls += 1
        self.pending_user_id = message_id or "u-disconnected"
        raise OpenCodeTransportDisconnected("POST", f"/session/{session_id}/message", ConnectionResetError("server disconnected"))

    async def get_session_status(self, timeout_seconds=30):
        return {"sessions": {"ses-1": {"state": "idle"}}}

    async def list_messages(self, session_id):
        if self.calls:
            return [
                {"id": self.pending_user_id, "role": "user", "parts": [{"type": "text", "text": "q"}]},
                {"id": "a-recovered-disconnect", "role": "assistant", "parts": [{"type": "text", "text": "Recovered after disconnect"}]},
            ]
        return []


class SubmitTransportDisconnectedStillRunningClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.calls = 0
        self.sent_texts: list[str] = []

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        self.calls += 1
        self.sent_texts.append(parts[0].get("text", ""))
        raise OpenCodeTransportDisconnected("POST", f"/session/{session_id}/message", ConnectionResetError("server disconnected"))

    async def get_session_status(self, timeout_seconds=30):
        return {"sessions": {"ses-1": {"state": "running"}}}

    async def list_messages(self, session_id):
        return []


@pytest.mark.asyncio
async def test_chat_submit_timeout_recovers_from_messages_without_502(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_TIMEOUT_RECOVERY_MAX_SECONDS", "0.05")
    monkeypatch.setenv("EFP_CHAT_TIMEOUT_RECOVERY_POLL_SECONDS", "0.001")
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=SubmitTimeoutRecoveredClient())))
    await client.start_server()
    response = await client.post("/api/chat", json={"message": "q", "session_id": "s-recovered", "request_id": "r-recovered"})
    payload = await response.json()
    assert response.status == 200
    assert payload["ok"] is True
    assert payload["completion_state"] == "completed"
    assert payload["response"] == "Recovered answer"
    types = [e.get("type") for e in payload["runtime_events"]]
    assert "chat.timeout_recovery.started" in types
    assert "chat.timeout_recovery.recovered" in types
    assert "execution.failed" not in types
    assert payload["_llm_debug"]["timeout_recovery"]["recovered"] is True
    await client.close()


@pytest.mark.asyncio
async def test_chat_submit_transport_disconnect_recovers_from_messages_without_502(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_TIMEOUT_RECOVERY_MAX_SECONDS", "0.05")
    monkeypatch.setenv("EFP_CHAT_TIMEOUT_RECOVERY_POLL_SECONDS", "0.001")
    fake = SubmitTransportDisconnectedRecoveredClient()
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=fake)))
    await client.start_server()
    response = await client.post("/api/chat", json={"message": "ORIGINAL USER PROMPT", "session_id": "s-disconnect-recovered", "request_id": "r-disconnect-recovered"})
    payload = await response.json()
    assert response.status == 200
    assert payload["ok"] is True
    assert payload["completion_state"] == "completed"
    assert payload["response"] == "Recovered after disconnect"
    assert fake.calls == 1
    types = [e.get("type") for e in payload["runtime_events"]]
    assert "chat.transport_recovery.started" in types
    assert "chat.transport_recovery.recovered" in types
    assert payload["_llm_debug"]["transport_recovery"]["recovered"] is True
    await client.close()


@pytest.mark.asyncio
async def test_chat_submit_transport_disconnect_still_running_keeps_recovering_not_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_TIMEOUT_RECOVERY_MAX_SECONDS", "0.01")
    monkeypatch.setenv("EFP_CHAT_TIMEOUT_RECOVERY_POLL_SECONDS", "0.001")
    monkeypatch.setenv("EFP_CHAT_AUTO_CONTINUE_ENABLED", "false")
    fake = SubmitTransportDisconnectedStillRunningClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()
    response = await client.post("/api/chat", json={"message": "ORIGINAL USER PROMPT", "session_id": "s-disconnect-running", "request_id": "r-disconnect-running"})
    payload = await response.json()
    assert response.status == 200
    assert payload["completion_state"] == "incomplete"
    assert payload["incomplete_reason"] == "opencode_transport_disconnected"
    assert fake.calls == 1
    assert fake.sent_texts == ["ORIGINAL USER PROMPT"]
    run = app[CHAT_RUN_STORE_KEY].get("r-disconnect-running")
    assert run.status in {"recovering", "stream_detached"}
    public = app[CHAT_RUN_STORE_KEY].to_public_dict(run)
    assert public["diagnostics"]["last_transport_error"]["exception_type"] == "ConnectionResetError"
    assert public["diagnostics"]["opencode_may_still_be_running"] is True
    types = [e.get("type") for e in payload["runtime_events"]]
    assert "chat.transport_recovery.started" in types
    assert "chat.transport_recovery.exhausted" in types
    await client.close()


class SubmitTimeoutExhaustedClient(FakeOpenCodeClient):
    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        raise OpenCodeTransportTimeout("POST", f"/session/{session_id}/message", 300, asyncio.TimeoutError())

    async def get_session_status(self, timeout_seconds=30):
        return {"sessions": {"ses-1": {"state": "running"}}}

    async def list_messages(self, session_id):
        return []


class SubmitTimeoutStillRunningAutoContinueClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.calls = 0
        self.sent_texts: list[str] = []

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        self.calls += 1
        self.sent_texts.append(parts[0].get("text", ""))
        if self.calls == 1:
            raise OpenCodeTransportTimeout("POST", f"/session/{session_id}/message", 300, asyncio.TimeoutError())
        return {"message": {"info": {"id": "a-cont", "role": "assistant"}, "parts": [{"type": "text", "text": "Recovered by explicit continuation"}]}}

    async def get_session_status(self, timeout_seconds=30):
        return {"sessions": {"ses-1": {"state": "running"}}}

    async def list_messages(self, session_id):
        return []


@pytest.mark.asyncio
async def test_chat_submit_timeout_recovery_exhausted_returns_incomplete(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_TIMEOUT_RECOVERY_MAX_SECONDS", "0.01")
    monkeypatch.setenv("EFP_CHAT_TIMEOUT_RECOVERY_POLL_SECONDS", "0.001")
    monkeypatch.setenv("EFP_CHAT_AUTO_CONTINUE_ENABLED", "false")
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=SubmitTimeoutExhaustedClient())))
    await client.start_server()
    response = await client.post("/api/chat", json={"message": "q", "session_id": "s-timeout-exhausted", "request_id": "r-timeout-exhausted"})
    payload = await response.json()
    assert response.status == 200
    assert payload["ok"] is False
    assert payload["completion_state"] == "incomplete"
    assert payload["incomplete_reason"] == "submit_timeout_recovery_exhausted"
    types = [e.get("type") for e in payload["runtime_events"]]
    assert "chat.timeout_recovery.started" in types
    assert "chat.timeout_recovery.poll" in types
    assert "chat.timeout_recovery.exhausted" in types
    assert "execution.failed" not in types
    await client.close()


@pytest.mark.asyncio
async def test_submit_timeout_recovery_exhausted_still_running_suppresses_auto_continue_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_TIMEOUT_RECOVERY_MAX_SECONDS", "0.01")
    monkeypatch.setenv("EFP_CHAT_TIMEOUT_RECOVERY_POLL_SECONDS", "0.001")
    monkeypatch.setenv("EFP_CHAT_AUTO_CONTINUE_ENABLED", "true")
    monkeypatch.setenv("EFP_CHAT_AUTO_CONTINUE_MAX_TURNS", "5")
    monkeypatch.setenv("EFP_CHAT_AUTO_CONTINUE_AFTER_RUNNING_TIMEOUT", "false")
    fake = SubmitTimeoutStillRunningAutoContinueClient()
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=fake)))
    await client.start_server()
    response = await client.post("/api/chat", json={"message": "ORIGINAL USER PROMPT", "session_id": "s-running-timeout", "request_id": "r-running-timeout"})
    payload = await response.json()
    assert response.status == 200
    assert payload["completion_state"] == "incomplete"
    assert "submit_timeout_recovery_exhausted" in payload["incomplete_reason"]
    assert payload["continuation_count"] == 0
    assert fake.calls == 1
    types = [e.get("type") for e in payload["runtime_events"]]
    assert "chat.timeout_recovery.exhausted" in types
    assert "continuation.suppressed" in types
    assert payload["metadata"]["diagnostics"]["opencode_may_still_be_running"] is True
    assert payload["metadata"]["auto_continue_suppressed_reason"] == "submit_timeout_recovery_exhausted_still_running"
    await client.close()


@pytest.mark.asyncio
async def test_submit_timeout_recovery_exhausted_still_running_can_continue_when_explicitly_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_TIMEOUT_RECOVERY_MAX_SECONDS", "0.01")
    monkeypatch.setenv("EFP_CHAT_TIMEOUT_RECOVERY_POLL_SECONDS", "0.001")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "0.005")
    monkeypatch.setenv("EFP_CHAT_AUTO_CONTINUE_ENABLED", "true")
    monkeypatch.setenv("EFP_CHAT_AUTO_CONTINUE_MAX_TURNS", "1")
    monkeypatch.setenv("EFP_CHAT_AUTO_CONTINUE_AFTER_RUNNING_TIMEOUT", "true")
    fake = SubmitTimeoutStillRunningAutoContinueClient()
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=fake)))
    await client.start_server()
    payload = await (await client.post("/api/chat", json={"message": "ORIGINAL USER PROMPT", "session_id": "s-running-timeout-enabled", "request_id": "r-running-timeout-enabled"})).json()
    assert payload["continuation_count"] >= 1
    assert fake.calls >= 2
    assert fake.sent_texts[0] == "ORIGINAL USER PROMPT"
    assert fake.sent_texts[1] != "ORIGINAL USER PROMPT"
    assert "Continue the same user request" in fake.sent_texts[1]
    started = next(e for e in payload["runtime_events"] if e.get("type") == "continuation.started")
    assert started["metadata"]["overlap_risk_acknowledged"] is True
    assert started["metadata"]["trigger_reason"] == "submit_timeout_recovery_exhausted_still_running"
    await client.close()


class WallTimeoutClient(FakeOpenCodeClient):
    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        return {"message": {"id": "a-progress", "role": "assistant", "parts": [{"type": "text", "text": "I am reading the repository..."}]}}

    async def list_messages(self, session_id):
        return [{"id": "a-progress", "role": "assistant", "parts": [{"type": "text", "text": "I am reading the repository..."}]}]


@pytest.mark.asyncio
async def test_chat_wall_timeout_returns_incomplete_not_502(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_TOTAL_WALL_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_POLL_SECONDS", "0.01")
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=WallTimeoutClient())))
    await client.start_server()
    response = await client.post("/api/chat", json={"message": "q", "session_id": "s-wall-timeout"})
    payload = await response.json()
    assert response.status == 200
    assert payload["ok"] is False
    assert payload["completion_state"] == "incomplete"
    assert payload["incomplete_reason"] == "wall_timeout"
    assert any(e.get("type") == "continuation.wall_timeout" for e in payload["runtime_events"])
    await client.close()


@pytest.mark.asyncio
async def test_chat_auto_continue_empty_timeout_then_final(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_POLL_SECONDS", "0.01")
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=EmptyTimeoutThenFinalClient())))
    await client.start_server()
    payload = await (await client.post("/api/chat", json={"message": "q", "session_id": "s-empty-final"})).json()
    assert payload["ok"] is True
    assert payload["completion_state"] == "completed"
    assert payload["continuation_count"] == 1
    assert payload["response"] == "Done. Summary..."
    assert not payload["incomplete_reason"]
    assert payload["_llm_debug"]["continuations"][-1]["completion_state"] == "completed"
    await client.close()


@pytest.mark.asyncio
async def test_chat_auto_continue_completed_clears_incomplete_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_POLL_SECONDS", "0.01")
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=EmptyTimeoutThenFinalClient())))
    await client.start_server()
    payload = await (await client.post("/api/chat", json={"message": "q", "session_id": "s-completed-reason"})).json()
    assert payload["completion_state"] == "completed"
    assert not payload["incomplete_reason"]
    assert payload["_llm_debug"]["continuations"][-1]["completion_state"] == "completed"
    await client.close()


class AlwaysIncompleteProgressClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        self.calls += 1
        return {
            "message": {
                "info": {"id": f"a-progress-{self.calls}", "role": "assistant"},
                "parts": [{"type": "text", "text": f"I am reading the repository... {self.calls}"}],
            }
        }

    async def list_messages(self, session_id):
        return []


class ContinuationPromptCaptureClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.calls = 0
        self.sent_texts: list[str] = []

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        self.calls += 1
        self.sent_texts.append(parts[0].get("text", ""))
        return {
            "message": {
                "info": {"id": f"a-progress-{self.calls}", "role": "assistant"},
                "parts": [{"type": "text", "text": "I am reading the repository..."}],
            }
        }

    async def list_messages(self, session_id):
        return []


@pytest.mark.asyncio
async def test_chat_auto_continue_uses_checkpoint_prompt_and_respects_configured_max_turns(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_POLL_SECONDS", "0.01")
    monkeypatch.setenv("EFP_CHAT_AUTO_CONTINUE_MAX_TURNS", "5")
    monkeypatch.setenv("EFP_CHAT_AUTO_CONTINUE_NO_PROGRESS_STOP", "false")
    fake = ContinuationPromptCaptureClient()
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=fake)))
    await client.start_server()
    payload = await (await client.post("/api/chat", json={"message": "ORIGINAL USER PROMPT", "session_id": "s-cont-prompt"})).json()
    assert payload["completion_state"] == "incomplete"
    assert payload["continuation_count"] == 5
    assert payload["incomplete_reason"] == "auto_continue_max_turns_reached"
    assert fake.sent_texts[0] == "ORIGINAL USER PROMPT"
    assert all(text != "ORIGINAL USER PROMPT" for text in fake.sent_texts[1:])
    assert all("Continue the same user request" in text for text in fake.sent_texts[1:])
    types = [e.get("type") for e in payload["runtime_events"]]
    assert "continuation.prompt_sent" in types
    assert "continuation.max_turns_reached" in types
    prompt_events = [e for e in payload["runtime_events"] if e.get("type") == "continuation.prompt_sent"]
    assert prompt_events
    assert all(e["metadata"]["is_original_user_prompt"] is False for e in prompt_events)
    assert all("ORIGINAL USER PROMPT" not in e["metadata"]["prompt_preview"] for e in prompt_events)
    assert all(len(e["metadata"]["prompt_preview"]) <= 500 for e in prompt_events)
    chatlog = await (await client.get("/api/sessions/s-cont-prompt/chatlog")).json()
    chatlog_types = [e.get("type") for e in chatlog["runtime_events"]]
    assert "continuation.prompt_sent" in chatlog_types
    assert "continuation.max_turns_reached" in chatlog_types
    await client.close()


@pytest.mark.asyncio
async def test_chat_auto_continue_max_turns_reached(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_POLL_SECONDS", "0.01")
    monkeypatch.setenv("EFP_CHAT_AUTO_CONTINUE_MAX_TURNS", "2")
    monkeypatch.setenv("EFP_CHAT_AUTO_CONTINUE_NO_PROGRESS_STOP", "false")
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=AlwaysIncompleteProgressClient())))
    await client.start_server()
    payload = await (await client.post("/api/chat", json={"message": "q", "session_id": "s-max-turns"})).json()
    assert payload["ok"] is False
    assert payload["completion_state"] == "incomplete"
    assert "auto_continue_max_turns_reached" in payload["incomplete_reason"]
    assert payload["continuation_count"] == 2
    max_turns_event = next(e for e in payload["runtime_events"] if e.get("type") == "continuation.max_turns_reached")
    assert max_turns_event["turn_index"] == 3
    assert max_turns_event["metadata"]["max_turns"] == 2
    assert payload["metadata"]["continuation"]["turns_attempted"] == 2
    await client.close()


class AutoContinueFailureClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        self.calls += 1
        if self.calls == 1:
            return {"message": {"info": {"id": "a-progress", "role": "assistant"}, "parts": [{"type": "text", "text": "I am reading the repository..."}]}}
        raise RuntimeError("continuation failed")

    async def list_messages(self, session_id):
        return []


@pytest.mark.asyncio
async def test_chat_auto_continue_failed_event(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_POLL_SECONDS", "0.01")
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=AutoContinueFailureClient())))
    await client.start_server()
    payload = await (await client.post("/api/chat", json={"message": "q", "session_id": "s-cont-fail"})).json()
    types = [e.get("type") for e in payload["runtime_events"]]
    assert "continuation.failed" in types
    failed_event = next(e for e in payload["runtime_events"] if e.get("type") == "continuation.failed")
    assert failed_event["state"] == "failed"
    assert failed_event["metadata"]["error_type"] == "RuntimeError"
    assert payload["metadata"]["continuation"]["debug"][-1]["state"] == "failed"
    assert not (payload["ok"] is True and payload["response"] == "")
    assert payload["ok"] is False
    assert payload["completion_state"] in {"incomplete", "error"}
    await client.close()


@pytest.mark.asyncio
async def test_chat_stream_disconnect_background_task_is_not_cancelled_and_gets_done_callback(caplog):
    caplog.set_level("ERROR")

    async def failing_background_chat():
        await asyncio.sleep(0)
        raise RuntimeError("background failed")

    task = asyncio.create_task(failing_background_chat())
    consumed = asyncio.Event()

    def consume(done):
        _consume_background_chat_task(done, request_id="r-stream", session_id="s-stream")
        consumed.set()

    task.add_done_callback(consume)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.wait_for(consumed.wait(), timeout=1)
    assert task.done()
    assert not task.cancelled()
    assert any(record.message == "Background chat task failed after stream disconnect" for record in caplog.records)


@pytest.mark.asyncio
async def test_chat_ignores_non_msg_payload_message_id(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_POLL_SECONDS", "0.01")
    c = AutoContinueClient()
    app = create_app(Settings.from_env(), opencode_client=c)
    monkeypatch.setattr(app[REQUEST_BINDING_STORE_KEY], "complete", lambda request_id: None)
    client = TestClient(TestServer(app)); await client.start_server()
    payload = await (await client.post("/api/chat", json={"message": "q", "session_id": "s-msg", "request_id": "r-msg", "message_id": "portal-user-xxx"})).json()
    assert payload["request_id"] == "r-msg"
    generated = next(mid for mid in c.sent_ids if isinstance(mid, str) and mid.startswith("msg"))
    assert "portal-user-xxx" not in c.sent_ids
    assert generated.startswith("msg")
    await client.close()
