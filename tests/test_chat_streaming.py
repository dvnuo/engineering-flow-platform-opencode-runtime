import json
from efp_opencode_adapter.app_keys import CHAT_RUN_STORE_KEY, EVENT_BUS_KEY, OPENCODE_CLIENT_KEY, REQUEST_BINDING_STORE_KEY
import asyncio
import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.opencode_client import OpenCodeClientError, OpenCodeTransportDisconnected, OpenCodeTransportTimeout
from efp_opencode_adapter.chat_api import SSEClientDisconnected, _safe_write_eof, _write_sse
from test_t06_helpers import FakeOpenCodeClient

class SlowFake(FakeOpenCodeClient):
    def __init__(self, fail=False):
        super().__init__(); self.entered=asyncio.Event(); self.release=asyncio.Event(); self.fail=fail
    async def send_message(self, session_id, **kwargs):
        self.entered.set(); await self.release.wait()
        if self.fail: raise OpenCodeClientError('boom')
        return {"messages":[{"role":"assistant","content":"ok"}]}


class StreamSubmitTimeoutRecoveredFake(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.timed_out = False
        self.user_message_id = ""

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        self.timed_out = True
        self.user_message_id = message_id or "u-timeout"
        raise OpenCodeTransportTimeout("POST", f"/session/{session_id}/message", 300, asyncio.TimeoutError())

    async def get_session_status(self, timeout_seconds=30):
        return {"sessions": {"ses-1": {"state": "idle"}}}

    async def list_messages(self, session_id):
        if self.timed_out:
            return [
                {"id": self.user_message_id, "role": "user", "parts": [{"type": "text", "text": "m"}]},
                {"id": "a-recovered", "role": "assistant", "parts": [{"type": "text", "text": "stream recovered"}]},
            ]
        return []


class StreamSubmitTransportDisconnectedRestartFake(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.disconnected = False
        self.user_message_id = ""

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        self.disconnected = True
        self.user_message_id = message_id or "u-disconnected"
        raise OpenCodeTransportDisconnected("POST", f"/session/{session_id}/message", ConnectionResetError("server disconnected"))

    async def get_session_status(self, timeout_seconds=30):
        return {"sessions": {"ses-1": {"state": "idle"}}}

    async def list_messages(self, session_id):
        if self.disconnected:
            return [
                {"id": self.user_message_id, "role": "user", "parts": [{"type": "text", "text": "m"}]},
                {"id": "a-recovered", "role": "assistant", "parts": [{"type": "text", "text": "stream transport recovered"}]},
            ]
        return []


class StreamRestartManager:
    def __init__(self):
        self.restart_calls = []

    def status_snapshot(self):
        return {"running": False, "returncode": 1}

    async def start(self, env=None, reason="startup"):
        return {"running": False, "returncode": 1, "last_restart_reason": reason}

    async def stop(self):
        return {"running": False}

    async def restart(self, env=None, reason="runtime_profile_apply"):
        self.restart_calls.append(reason)
        return {"running": True, "pid": 456, "health_ok": True, "last_restart_reason": reason}


class StreamAutoContinueFake(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        self.calls += 1
        if self.calls == 1:
            return {"message": {"info": {"id": "a-progress", "role": "assistant"}, "parts": [{"type": "text", "text": "I am reading the repository..."}]}}
        return {"message": {"info": {"id": "a-final", "role": "assistant"}, "parts": [{"type": "text", "text": "final stream answer"}]}}


def _sse_events(body: str) -> list[tuple[str, dict]]:
    events = []
    for chunk in body.strip().split("\n\n"):
        event_name = ""
        data = ""
        for line in chunk.splitlines():
            if line.startswith("event: "):
                event_name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data = line.removeprefix("data: ")
        if event_name and data:
            events.append((event_name, json.loads(data)))
    return events


async def _wait_for_run(app, request_id: str, predicate, *, timeout: float = 2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    last_run = None
    while asyncio.get_running_loop().time() < deadline:
        last_run = app[CHAT_RUN_STORE_KEY].get(request_id)
        if last_run is not None and predicate(last_run):
            return last_run
        await asyncio.sleep(0.02)
    raise AssertionError(f"run predicate never matched: {last_run}")


async def _finish_maybe_cancelled_post(task: asyncio.Task, *, timeout: float = 1.0):
    try:
        return await asyncio.wait_for(task, timeout=timeout)
    except asyncio.CancelledError:
        return None
    except Exception:
        return None

@pytest.mark.asyncio
async def test_chat_stream_forwards_runtime_event_before_final(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    fake=SlowFake(); app=create_app(Settings.from_env(), opencode_client=fake); c=TestClient(TestServer(app)); await c.start_server()
    t=asyncio.create_task(c.post('/api/chat/stream', json={'message':'m','session_id':'portal-stream-1','request_id':'req-stream-1'}))
    await fake.entered.wait()
    await app[EVENT_BUS_KEY].publish({'type':'tool.started','session_id':'portal-stream-1','request_id':'req-stream-1','tool':'efp_test_tool','engine':'opencode','raw_type':'tool.start'})
    fake.release.set(); resp=await t; body=await resp.text()
    assert body.index('tool.started') < body.index('event: final')
    await c.close()


@pytest.mark.asyncio
async def test_chat_stream_emits_started_heartbeat_runtime_event_and_final(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = SlowFake()
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        task = asyncio.create_task(c.post("/api/chat/stream", json={"message": "m", "session_id": "portal-stream-start", "request_id": "req-stream-start"}))
        await fake.entered.wait()
        await app[EVENT_BUS_KEY].publish({"type": "tool.started", "session_id": "portal-stream-start", "request_id": "req-stream-start", "tool": "efp_test_tool", "engine": "opencode"})
        fake.release.set()
        resp = await task
        body = await resp.text()
        events = _sse_events(body)
        names = [name for name, _payload in events]
        assert names[:4] == ["chat.started", "chat.stream_attached", "heartbeat", "runtime_event"]
        started_payload = events[0][1]
        assert started_payload["session_id"] == "portal-stream-start"
        assert started_payload["request_id"] == "req-stream-start"
        assert started_payload["chatlog_id"] == "req-stream-start"
        attached_payload = events[1][1]
        assert attached_payload["event_type"] == "chat.stream_attached"
        heartbeat_payload = events[2][1]
        assert heartbeat_payload["completion_state"] == "running"
        assert "elapsed_seconds" in heartbeat_payload
        assert "last_event_at" in heartbeat_payload
        assert "tool.started" in body
        final_payload = next(payload for name, payload in events if name == "final")
        assert final_payload["completion_state"]
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_chat_stream_emits_timeout_recovery_before_final(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_TIMEOUT_RECOVERY_MAX_SECONDS", "0.05")
    monkeypatch.setenv("EFP_CHAT_TIMEOUT_RECOVERY_POLL_SECONDS", "0.001")
    c = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=StreamSubmitTimeoutRecoveredFake())))
    await c.start_server()
    try:
        resp = await c.post("/api/chat/stream", json={"message": "m", "session_id": "portal-stream-timeout", "request_id": "req-stream-timeout"})
        body = await resp.text()
        assert "chat.timeout_recovery.started" in body
        assert body.index("chat.timeout_recovery.started") < body.index("event: final")
        events = _sse_events(body)
        final_payload = next(payload for name, payload in events if name == "final")
        assert final_payload["completion_state"] == "completed"
        assert final_payload["response"] == "stream recovered"
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_chat_stream_emits_transport_recovery_and_restart_before_final(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_TIMEOUT_RECOVERY_MAX_SECONDS", "0.05")
    monkeypatch.setenv("EFP_CHAT_TIMEOUT_RECOVERY_POLL_SECONDS", "0.001")
    manager = StreamRestartManager()
    c = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=StreamSubmitTransportDisconnectedRestartFake(), opencode_process_manager=manager)))
    await c.start_server()
    try:
        resp = await c.post("/api/chat/stream", json={"message": "m", "session_id": "portal-stream-disconnect", "request_id": "req-stream-disconnect"})
        body = await resp.text()
        assert "chat.transport_recovery.started" in body
        assert "opencode.process.restarted" in body
        assert body.index("chat.transport_recovery.started") < body.index("event: final")
        assert manager.restart_calls == ["transport_disconnected"]
        events = _sse_events(body)
        final_payload = next(payload for name, payload in events if name == "final")
        assert final_payload["completion_state"] == "completed"
        assert final_payload["response"] == "stream transport recovered"
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_chat_stream_emits_continuation_events_before_final(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_POLL_SECONDS", "0.01")
    c = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=StreamAutoContinueFake())))
    await c.start_server()
    try:
        resp = await c.post("/api/chat/stream", json={"message": "m", "session_id": "portal-stream-continuation", "request_id": "req-stream-continuation"})
        body = await resp.text()
        assert "continuation.started" in body
        assert "continuation.prompt_sent" in body
        assert "continuation.completed" in body
        assert body.index("continuation.started") < body.index("event: final")
        events = _sse_events(body)
        final_payload = next(payload for name, payload in events if name == "final")
        assert final_payload["completion_state"] == "completed"
        assert final_payload["metadata"]["continuation"]["turns_attempted"] == 1
    finally:
        await c.close()

@pytest.mark.asyncio
async def test_chat_stream_sends_delta_event_for_assistant_delta(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    fake=SlowFake(); app=create_app(Settings.from_env(), opencode_client=fake); c=TestClient(TestServer(app)); await c.start_server()
    t=asyncio.create_task(c.post('/api/chat/stream', json={'message':'m','session_id':'portal-stream-2','request_id':'req-stream-2'}))
    await fake.entered.wait()
    await app[EVENT_BUS_KEY].publish({'type':'assistant_delta','session_id':'portal-stream-2','request_id':'req-stream-2','data':{'delta':'hello delta'},'engine':'opencode','raw_type':'message.part.delta'})
    fake.release.set(); resp=await t; body=await resp.text()
    assert 'event: delta' in body and 'hello delta' in body and body.index('event: delta') < body.index('event: final')
    await c.close()

@pytest.mark.asyncio
async def test_chat_stream_unsubscribes_on_error(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    fake=SlowFake(fail=True); app=create_app(Settings.from_env(), opencode_client=fake); c=TestClient(TestServer(app)); await c.start_server()
    t=asyncio.create_task(c.post('/api/chat/stream', json={'message':'m','session_id':'portal-stream-3','request_id':'req-stream-3'}))
    await fake.entered.wait(); fake.release.set(); resp=await t; body=await resp.text()
    assert 'event: error' in body
    assert len(app[EVENT_BUS_KEY]._subs) == 0
    await c.close()


@pytest.mark.asyncio
async def test_chat_stream_respects_explicit_portal_request_id(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    fake=SlowFake(); app=create_app(Settings.from_env(), opencode_client=fake); c=TestClient(TestServer(app)); await c.start_server()
    t=asyncio.create_task(c.post('/api/chat/stream', json={'message':'m','session_id':'portal-stream-3','request_id':'req-stream-3'}))
    await fake.entered.wait()
    await app[EVENT_BUS_KEY].publish({'type':'assistant_delta','session_id':'portal-stream-3','portal_request_id':'other-request','data':{'delta':'should not appear'}})
    fake.release.set(); resp=await t; body=await resp.text()
    assert 'should not appear' not in body
    await c.close()


@pytest.mark.asyncio
async def test_chat_stream_does_not_forward_raw_event_from_other_request(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = SlowFake()
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        task = asyncio.create_task(c.post("/api/chat/stream", json={"message": "m", "session_id": "s-stream-isolation", "request_id": "req-current"}))
        await fake.entered.wait()
        await app[EVENT_BUS_KEY].publish({
            "type": "message.delta",
            "session_id": "s-stream-isolation",
            "request_id": "req-other",
            "raw_type": "message.part.delta",
            "data": {"delta": "SHOULD_NOT_APPEAR", "message_role": "assistant", "raw_type": "message.part.delta"},
        })
        await app[EVENT_BUS_KEY].publish({
            "type": "message.delta",
            "session_id": "s-stream-isolation",
            "request_id": "req-current",
            "raw_type": "message.part.delta",
            "data": {"delta": "SHOULD_APPEAR", "message_role": "assistant", "raw_type": "message.part.delta"},
        })
        fake.release.set()
        resp = await task
        body = await resp.text()
        assert "SHOULD_APPEAR" in body
        assert "SHOULD_NOT_APPEAR" not in body
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_chat_stream_allows_raw_event_when_exact_binding_matches_current_request(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = SlowFake()
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[REQUEST_BINDING_STORE_KEY].bind_message("ses-1", "msg-current", "s-stream-binding", "req-current")
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        task = asyncio.create_task(c.post("/api/chat/stream", json={"message": "m", "session_id": "s-stream-binding", "request_id": "req-current"}))
        await fake.entered.wait()
        await app[EVENT_BUS_KEY].publish({
            "type": "message.delta",
            "session_id": "s-stream-binding",
            "request_id": "msg-current",
            "opencode_session_id": "ses-1",
            "raw_type": "message.part.delta",
            "data": {"message_id": "msg-current", "delta": "BOUND_DELTA", "message_role": "assistant"},
        })
        fake.release.set()
        resp = await task
        body = await resp.text()
        assert "BOUND_DELTA" in body
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_chat_stream_drops_request_scoped_event_without_request_id(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = SlowFake()
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        task = asyncio.create_task(c.post("/api/chat/stream", json={"message": "m", "session_id": "s-stream-isolation", "request_id": "req-current"}))
        await fake.entered.wait()
        await app[EVENT_BUS_KEY].publish({
            "type": "tool.completed",
            "session_id": "s-stream-isolation",
            "raw_type": "tool.complete",
            "data": {"tool": "bash"},
        })
        fake.release.set()
        resp = await task
        body = await resp.text()
        assert "tool.completed" not in body
        assert "\"tool\": \"bash\"" not in body
    finally:
        await c.close()

@pytest.mark.asyncio
async def test_chat_stream_immediate_error_does_not_wait_for_heartbeat(tmp_path, monkeypatch):
    import time
    import efp_opencode_adapter.chat_api as chat_api
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    monkeypatch.setattr(chat_api, 'STREAM_HEARTBEAT_SECONDS', 15.0)
    app=create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    c=TestClient(TestServer(app)); await c.start_server()
    t0=time.monotonic()
    resp=await c.post('/api/chat/stream', json={'message':'hello','metadata':{'runtime_profile':'bad'}})
    body=await resp.text(); elapsed=time.monotonic()-t0
    assert 'event: error' in body and 'runtime_profile_must_be_object' in body
    assert elapsed < 1.0
    await c.close()

class RaceFake(FakeOpenCodeClient):
    async def send_message(self, session_id, **kwargs):
        await self._bus.publish({'type':'tool.completed','session_id':'portal-race-1','request_id':'req-race-1','tool':'efp_race_tool','raw_type':'tool.complete'})
        return {"messages":[{"role":"assistant","content":"ok"}]}


class DisconnectingFake(SlowFake):
    def __init__(self):
        super().__init__(fail=False)
        self.finished = asyncio.Event()
        self.was_cancelled = False

    async def send_message(self, session_id, **kwargs):
        try:
            result = await super().send_message(session_id, **kwargs)
            self.finished.set()
            return result
        except asyncio.CancelledError:
            self.was_cancelled = True
            raise

@pytest.mark.asyncio
async def test_chat_stream_drains_event_published_at_completion_before_final(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    app=create_app(Settings.from_env(), opencode_client=RaceFake())
    app[OPENCODE_CLIENT_KEY]._bus = app[EVENT_BUS_KEY]
    c=TestClient(TestServer(app)); await c.start_server()
    try:
        resp=await c.post('/api/chat/stream', json={'message':'hello','session_id':'portal-race-1','request_id':'req-race-1'})
        body=await resp.text()
        assert 'tool.completed' in body
        boundary = body.find('event: final')
        if boundary < 0:
            boundary = body.find('event: done')
        if boundary < 0:
            boundary = body.find('event: error')
        assert boundary > 0
        assert body.index('tool.completed') < boundary
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_write_sse_raises_sse_disconnected_on_connection_reset():
    class Resp:
        async def write(self, *_args, **_kwargs):
            raise ConnectionResetError("Cannot write to closing transport")

    with pytest.raises(SSEClientDisconnected):
        await _write_sse(Resp(), "runtime_event", {"ok": True})


@pytest.mark.asyncio
async def test_write_sse_raises_sse_disconnected_on_broken_pipe():
    class Resp:
        async def write(self, *_args, **_kwargs):
            raise BrokenPipeError("broken pipe")

    with pytest.raises(SSEClientDisconnected):
        await _write_sse(Resp(), "runtime_event", {"ok": True})


@pytest.mark.asyncio
async def test_safe_write_eof_ignores_disconnect_errors():
    class ConnResetResp:
        async def write_eof(self):
            raise ConnectionResetError("closed")

    class BrokenPipeResp:
        async def write_eof(self):
            raise BrokenPipeError("broken pipe")

    await _safe_write_eof(ConnResetResp())
    await _safe_write_eof(BrokenPipeResp())


@pytest.mark.asyncio
async def test_safe_write_eof_ignores_closed_transport_runtime_error_and_raises_unrelated():
    class ClosingResp:
        async def write_eof(self):
            raise RuntimeError("Cannot write to closing transport")

    class ClosedResp:
        async def write_eof(self):
            raise RuntimeError("already closed")

    class UnrelatedResp:
        async def write_eof(self):
            raise RuntimeError("boom")

    await _safe_write_eof(ClosingResp())
    await _safe_write_eof(ClosedResp())
    with pytest.raises(RuntimeError, match="boom"):
        await _safe_write_eof(UnrelatedResp())


@pytest.mark.asyncio
async def test_chat_stream_client_disconnect_does_not_cancel_run_task_or_leak_subscriptions(tmp_path, monkeypatch):
    import efp_opencode_adapter.chat_api as chat_api
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    fake = DisconnectingFake()
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app))
    await c.start_server()
    original_write_sse = chat_api._write_sse
    write_count = 0

    async def flaky_write(resp, event_name, payload):
        nonlocal write_count
        write_count += 1
        if write_count == 2:
            raise chat_api.SSEClientDisconnected()
        return await original_write_sse(resp, event_name, payload)

    monkeypatch.setattr(chat_api, "_write_sse", flaky_write)
    try:
        t = asyncio.create_task(c.post('/api/chat/stream', json={'message': 'm', 'session_id': 'portal-disconnect-1', 'request_id': 'req-disconnect-1'}))
        await fake.entered.wait()
        await app[EVENT_BUS_KEY].publish({'type': 'assistant_delta', 'session_id': 'portal-disconnect-1', 'request_id': 'req-disconnect-1', 'data': {'delta': 'hello'}})
        fake.release.set()
        resp = await t
        assert resp.status == 200
        await resp.release()
        await asyncio.wait_for(fake.finished.wait(), timeout=1.0)
        assert fake.was_cancelled is False
        assert len(app[EVENT_BUS_KEY]._subs) == 0
        for _ in range(20):
            run = app[CHAT_RUN_STORE_KEY].get("req-disconnect-1")
            if run.status == "completed":
                break
            await asyncio.sleep(0.05)
        assert run.stream_state == "closed"
        assert run.status == "completed"
    finally:
        monkeypatch.setattr(chat_api, "_write_sse", original_write_sse)
        await c.close()


@pytest.mark.asyncio
async def test_chat_stream_detaches_run_on_client_disconnect_before_completion(tmp_path, monkeypatch):
    import efp_opencode_adapter.chat_api as chat_api

    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = DisconnectingFake()
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app))
    await c.start_server()
    original_write_sse = chat_api._write_sse

    async def flaky_write(resp, event_name, payload):
        if event_name == "heartbeat":
            raise chat_api.SSEClientDisconnected()
        return await original_write_sse(resp, event_name, payload)

    monkeypatch.setattr(chat_api, "_write_sse", flaky_write)
    try:
        task = asyncio.create_task(c.post("/api/chat/stream", json={"message": "m", "session_id": "portal-detach", "request_id": "req-detach"}))
        await fake.entered.wait()
        resp = await task
        assert resp.status == 200
        run = app[CHAT_RUN_STORE_KEY].get("req-detach")
        assert run.status == "stream_detached"
        assert run.stream_state == "detached"
        assert run.status != "failed"

        fake.release.set()
        await asyncio.wait_for(fake.finished.wait(), timeout=1.0)
        for _ in range(20):
            run = app[CHAT_RUN_STORE_KEY].get("req-detach")
            if run.status == "completed":
                break
            await asyncio.sleep(0.05)
        assert run.status == "completed"
        assert run.final_payload["response"] == "ok"
    finally:
        monkeypatch.setattr(chat_api, "_write_sse", original_write_sse)
        await c.close()


@pytest.mark.asyncio
async def test_chat_stream_handler_cancelled_marks_detached_and_keeps_background_run(tmp_path, monkeypatch):
    import efp_opencode_adapter.chat_api as chat_api

    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = DisconnectingFake()
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app))
    await c.start_server()
    original_write_sse = chat_api._write_sse

    async def cancelling_write(resp, event_name, payload):
        if event_name == "heartbeat":
            raise asyncio.CancelledError()
        return await original_write_sse(resp, event_name, payload)

    monkeypatch.setattr(chat_api, "_write_sse", cancelling_write)
    try:
        task = asyncio.create_task(c.post("/api/chat/stream", json={"message": "m", "session_id": "portal-cancel", "request_id": "req-cancel"}))
        await asyncio.wait_for(fake.entered.wait(), timeout=1)
        await _finish_maybe_cancelled_post(task)

        run = await _wait_for_run(app, "req-cancel", lambda item: item.stream_state == "detached")
        assert run.status in {"running", "stream_detached"}
        assert run.stream_state == "detached"
        assert run.metadata["stream_detach_reason"] == "handler_cancelled"
        assert run.metadata["transport_cancelled"] is True
        assert run.metadata["background_continues"] is True
        assert fake.was_cancelled is False

        replayed = app[EVENT_BUS_KEY].recent_events(request_id="req-cancel")
        detached = [event for event in replayed if event["type"] == "chat.stream_detached"]
        assert detached
        assert detached[-1]["data"]["reason"] == "handler_cancelled"
        assert detached[-1]["data"]["metadata"]["transport_cancelled"] is True

        fake.release.set()
        await asyncio.wait_for(fake.finished.wait(), timeout=1)
        final_run = await _wait_for_run(app, "req-cancel", lambda item: item.status == "completed")
        assert final_run.final_payload["response"] == "ok"
    finally:
        monkeypatch.setattr(chat_api, "_write_sse", original_write_sse)
        await c.close()


@pytest.mark.asyncio
async def test_chat_stream_client_disconnect_does_not_mark_run_incomplete(tmp_path, monkeypatch):
    import efp_opencode_adapter.chat_api as chat_api

    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = DisconnectingFake()
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app))
    await c.start_server()
    original_write_sse = chat_api._write_sse

    async def disconnecting_write(resp, event_name, payload):
        if event_name == "heartbeat":
            raise chat_api.SSEClientDisconnected()
        return await original_write_sse(resp, event_name, payload)

    monkeypatch.setattr(chat_api, "_write_sse", disconnecting_write)
    try:
        task = asyncio.create_task(c.post("/api/chat/stream", json={"message": "m", "session_id": "portal-no-incomplete", "request_id": "req-no-incomplete"}))
        await asyncio.wait_for(fake.entered.wait(), timeout=1)
        resp = await task
        assert resp.status == 200

        detached_run = await _wait_for_run(app, "req-no-incomplete", lambda item: item.stream_state == "detached")
        assert detached_run.status in {"running", "stream_detached"}
        assert detached_run.incomplete_reason != "Stream ended before a final assistant response."
        assert detached_run.metadata["stream_detach_reason"] == "client_disconnected"

        fake.release.set()
        await asyncio.wait_for(fake.finished.wait(), timeout=1)
        completed_run = await _wait_for_run(app, "req-no-incomplete", lambda item: item.status == "completed")
        assert completed_run.incomplete_reason == ""

        run_resp = await c.get("/api/chat/runs/req-no-incomplete")
        run_payload = await run_resp.json()
        assert run_payload["run"]["status"] == "completed"
        assert run_payload["run"]["final_payload"]["response"] == "ok"
    finally:
        monkeypatch.setattr(chat_api, "_write_sse", original_write_sse)
        await c.close()


@pytest.mark.asyncio
async def test_chat_stream_detached_background_completion_replayable(tmp_path, monkeypatch):
    import efp_opencode_adapter.chat_api as chat_api

    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = DisconnectingFake()
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app))
    await c.start_server()
    original_write_sse = chat_api._write_sse

    async def disconnecting_write(resp, event_name, payload):
        if event_name == "heartbeat":
            raise chat_api.SSEClientDisconnected()
        return await original_write_sse(resp, event_name, payload)

    monkeypatch.setattr(chat_api, "_write_sse", disconnecting_write)
    try:
        task = asyncio.create_task(c.post("/api/chat/stream", json={"message": "m", "session_id": "portal-replay-detached", "request_id": "req-replay-detached"}))
        await asyncio.wait_for(fake.entered.wait(), timeout=1)
        resp = await task
        assert resp.status == 200
        await _wait_for_run(app, "req-replay-detached", lambda item: item.stream_state == "detached")

        fake.release.set()
        await asyncio.wait_for(fake.finished.wait(), timeout=1)
        await _wait_for_run(app, "req-replay-detached", lambda item: item.status == "completed")

        ws = await c.ws_connect("/api/events?request_id=req-replay-detached&replay=1")
        assert (await ws.receive_json())["type"] == "connected"
        replay_types = []
        for _ in range(20):
            event = await ws.receive_json(timeout=2)
            replay_types.append(event["type"])
            if {"chat.stream_detached", "chat.run.completed", "assistant.message.completed"}.issubset(set(replay_types)):
                break
        await ws.close()
        assert "chat.stream_detached" in replay_types
        assert "chat.run.completed" in replay_types
        assert "assistant.message.completed" in replay_types

        run_resp = await c.get("/api/chat/runs/req-replay-detached")
        run_payload = await run_resp.json()
        assert run_payload["run"]["status"] == "completed"
        assert run_payload["run"]["final_payload"]["response"] == "ok"
    finally:
        monkeypatch.setattr(chat_api, "_write_sse", original_write_sse)
        await c.close()


@pytest.mark.asyncio
async def test_internal_stream_failure_still_cancels_background_run(tmp_path, monkeypatch):
    import efp_opencode_adapter.chat_api as chat_api

    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = DisconnectingFake()
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app))
    await c.start_server()
    original_write_sse = chat_api._write_sse

    async def broken_write(resp, event_name, payload):
        if event_name == "runtime_event" and isinstance(payload, dict) and payload.get("type") == "tool.started":
            raise RuntimeError("stream bug")
        return await original_write_sse(resp, event_name, payload)

    monkeypatch.setattr(chat_api, "_write_sse", broken_write)
    try:
        task = asyncio.create_task(c.post("/api/chat/stream", json={"message": "m", "session_id": "portal-stream-bug", "request_id": "req-stream-bug"}))
        await asyncio.wait_for(fake.entered.wait(), timeout=1)
        await app[EVENT_BUS_KEY].publish({"type": "tool.started", "session_id": "portal-stream-bug", "request_id": "req-stream-bug", "tool": "bug"})
        await _finish_maybe_cancelled_post(task)
        for _ in range(20):
            if fake.was_cancelled:
                break
            await asyncio.sleep(0.02)
        assert fake.was_cancelled is True

        run = app[CHAT_RUN_STORE_KEY].get("req-stream-bug")
        assert run.stream_state != "detached"
        replayed_types = [event["type"] for event in app[EVENT_BUS_KEY].recent_events(request_id="req-stream-bug")]
        assert "chat.stream_detached" not in replayed_types
    finally:
        monkeypatch.setattr(chat_api, "_write_sse", original_write_sse)
        fake.release.set()
        await c.close()


@pytest.mark.asyncio
async def test_chat_stream_attached_and_final_close_run(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        resp = await c.post("/api/chat/stream", json={"message": "hello", "session_id": "portal-close", "request_id": "req-close"})
        body = await resp.text()
        assert "event: chat.stream_attached" in body
        run = app[CHAT_RUN_STORE_KEY].get("req-close")
        assert run.status == "completed"
        assert run.stream_state == "closed"
        assert run.final_payload["response"] == "echo: hello"
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_chat_stream_emits_assistant_message_updated_before_final(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = SlowFake()
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        task = asyncio.create_task(c.post("/api/chat/stream", json={"message": "m", "session_id": "portal-projection", "request_id": "req-projection"}))
        await fake.entered.wait()
        await app[EVENT_BUS_KEY].publish(
            {
                "type": "message.delta",
                "session_id": "portal-projection",
                "request_id": "req-projection",
                "opencode_session_id": "ses-1",
                "raw_type": "message.part.delta",
                "data": {"delta": "visible", "message_role": "assistant", "part_type": "text", "message_id": "a-live"},
            }
        )
        fake.release.set()
        resp = await task
        body = await resp.text()
        assert "assistant.message.updated" in body
        assert body.index("assistant.message.updated") < body.index("event: final")
        run = app[CHAT_RUN_STORE_KEY].get("req-projection")
        assert "visible" in run.last_response_text or run.status == "completed"
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_stream_error_response_swallow_sse_client_disconnect(monkeypatch):
    import efp_opencode_adapter.chat_api as chat_api
    app = web.Application()

    async def handler(request):
        return await chat_api._stream_error_response(request, "invalid_json")

    app.router.add_get("/error", handler)
    c = TestClient(TestServer(app))
    await c.start_server()
    original_write_sse = chat_api._write_sse
    original_safe_write_eof = chat_api._safe_write_eof
    eof_called = False

    async def fake_write_sse(*_args, **_kwargs):
        raise chat_api.SSEClientDisconnected()

    async def fake_safe_write_eof(*_args, **_kwargs):
        nonlocal eof_called
        eof_called = True

    monkeypatch.setattr(chat_api, "_write_sse", fake_write_sse)
    monkeypatch.setattr(chat_api, "_safe_write_eof", fake_safe_write_eof)
    try:
        resp = await c.get("/error")
        assert resp.status == 200
        await resp.release()
        assert eof_called is False
    finally:
        monkeypatch.setattr(chat_api, "_write_sse", original_write_sse)
        monkeypatch.setattr(chat_api, "_safe_write_eof", original_safe_write_eof)
        await c.close()


@pytest.mark.asyncio
async def test_chat_stream_filters_noise_events_and_keeps_useful_events(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    fake=SlowFake(); app=create_app(Settings.from_env(), opencode_client=fake); c=TestClient(TestServer(app)); await c.start_server()
    t=asyncio.create_task(c.post('/api/chat/stream', json={'message':'m','session_id':'portal-stream-filter-1','request_id':'req-stream-filter-1'}))
    await fake.entered.wait()
    for evt in [
        {'type':'opencode.sync','session_id':'portal-stream-filter-1'},
        {'type':'session.updated','session_id':'portal-stream-filter-1'},
        {'type':'opencode.step.finished','session_id':'portal-stream-filter-1'},
        {'type':'unknown.debug','session_id':'portal-stream-filter-1','request_id':''},
        {'type':'message.delta','session_id':'portal-stream-filter-1','request_id':'req-stream-filter-1','raw_type':'message.part.updated','data':{'delta':'Hi'}},
        {'type':'message.delta','session_id':'portal-stream-filter-1','request_id':'req-stream-filter-1','raw_type':'message.part.delta','data':{'delta':'Yo','message_role':'assistant','part_type':'text'}},
        {'type':'llm_thinking','session_id':'portal-stream-filter-1','request_id':'req-stream-filter-1','data':{'message':'thinking'}},
    ]:
        await app[EVENT_BUS_KEY].publish(evt)
    fake.release.set(); resp=await t; body=await resp.text()
    assert 'opencode.sync' not in body
    assert 'session.updated' not in body
    assert 'opencode.step.finished' not in body
    assert 'unknown.debug' not in body
    assert '"raw_type": "message.part.updated"' in body
    assert 'event: delta' in body and 'Yo' in body
    assert 'event: delta\ndata: {"delta": "Hi"' not in body
    assert 'llm_thinking' in body
    await c.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("completion_state", "ok"),
    [
        ("completed", True),
        ("blocked", False),
        ("error", False),
        ("incomplete", False),
        ("empty_final", False),
    ],
)
async def test_chat_stream_final_payload_has_completion_state_contract(tmp_path, monkeypatch, completion_state, ok):
    import efp_opencode_adapter.chat_api as chat_api

    async def fake_handle_chat_payload(payload, app):
        return {
            "ok": ok,
            "completion_state": completion_state,
            "response": f"state={completion_state}",
            "session_id": payload.get("session_id", "s-state"),
            "request_id": payload.get("request_id", "r-state"),
            "runtime_events": [],
        }

    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(chat_api, "handle_chat_payload", fake_handle_chat_payload)
    c = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())))
    await c.start_server()
    try:
        resp = await c.post("/api/chat/stream", json={"message": "m", "session_id": "s-state", "request_id": f"r-{completion_state}"})
        body = await resp.text()
        assert "event: final" in body
        assert f"\"completion_state\": \"{completion_state}\"" in body
        assert f"\"ok\": {str(ok).lower()}" in body
        assert f"\"response\": \"state={completion_state}\"" in body
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_chat_stream_does_not_duplicate_real_and_synthetic_delta(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    fake=SlowFake(); app=create_app(Settings.from_env(), opencode_client=fake); c=TestClient(TestServer(app)); await c.start_server()
    t=asyncio.create_task(c.post('/api/chat/stream', json={'message':'m','session_id':'portal-stream-dup-1','request_id':'req-stream-dup-1'}))
    await fake.entered.wait()
    await app[EVENT_BUS_KEY].publish({'type':'message.delta','session_id':'portal-stream-dup-1','request_id':'req-stream-dup-1','raw_type':'message.part.delta','data':{'delta':'Hi','message_role':'assistant','part_type':'text'}})
    await app[EVENT_BUS_KEY].publish({'type':'assistant_delta','session_id':'portal-stream-dup-1','request_id':'req-stream-dup-1','synthetic_final_delta':True,'data':{'delta':'Hi','synthetic_final_delta':True}})
    fake.release.set(); resp=await t; body=await resp.text()
    assert body.count('event: delta') == 1
    await c.close()


@pytest.mark.asyncio
async def test_chat_stream_blocks_message_part_updated_delta_echo(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    fake=SlowFake(); app=create_app(Settings.from_env(), opencode_client=fake); c=TestClient(TestServer(app)); await c.start_server()
    t=asyncio.create_task(c.post('/api/chat/stream', json={'message':'m','session_id':'portal-stream-echo-1','request_id':'req-stream-echo-1'}))
    await fake.entered.wait()
    await app[EVENT_BUS_KEY].publish({'type':'message.delta','session_id':'portal-stream-echo-1','request_id':'req-stream-echo-1','raw_type':'message.part.updated','data':{'delta':'hi'}})
    fake.release.set(); resp=await t; body=await resp.text()
    assert 'event: delta\ndata: {"delta": "hi"' not in body
    await c.close()


@pytest.mark.asyncio
async def test_chat_stream_blocks_message_part_delta_without_assistant_role(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = SlowFake()
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        t = asyncio.create_task(c.post("/api/chat/stream", json={"message": "m", "session_id": "portal-stream-missing-role-1", "request_id": "req-stream-missing-role-1"}))
        await fake.entered.wait()
        await app[EVENT_BUS_KEY].publish({"type": "message.delta", "session_id": "portal-stream-missing-role-1", "request_id": "req-stream-missing-role-1", "raw_type": "message.part.delta", "data": {"delta": "hi"}})
        fake.release.set()
        resp = await t
        body = await resp.text()
        assert 'event: delta\ndata: {"delta": "hi"' not in body
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_chat_stream_final_payload_includes_assistant_message_ids(tmp_path, monkeypatch):
    import efp_opencode_adapter.chat_api as chat_api

    async def fake_handle_chat_payload(request, payload):
        return {
            "ok": True,
            "completion_state": "completed",
            "response": "done",
            "session_id": "s-final",
            "request_id": "r-final",
            "assistant_message_id": "a-2",
            "assistant_message_ids": ["a-1", "a-2"],
            "runtime_events": [],
            "events": [],
            "user_message_id": "u-1",
        }

    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(chat_api, "handle_chat_payload", fake_handle_chat_payload)
    c = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())))
    await c.start_server()
    try:
        resp = await c.post("/api/chat/stream", json={"message": "m", "session_id": "s-final", "request_id": "r-final"})
        body = await resp.text()
        assert 'event: final' in body
        assert '"assistant_message_id": "a-2"' in body
        assert '"assistant_message_ids": ["a-1", "a-2"]' in body
        assert 'event: done\ndata: {"ok": true}' in body
    finally:
        await c.close()


class FragmentStreamClient(FakeOpenCodeClient):
    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        text = parts[0].get("text", "")
        self.messages[session_id].append({"info": {"id": "u-new", "role": "user"}, "parts": [{"type": "text", "text": text}]})
        self.messages[session_id].append({"info": {"id": "a-frag-1", "role": "assistant"}, "parts": [{"type": "text", "text": "part 1"}]})
        self.messages[session_id].append({"info": {"id": "a-frag-2", "role": "assistant"}, "parts": [{"type": "text", "text": "part 2"}]})
        return {"message": {"info": {"id": "a-frag-2", "role": "assistant"}, "parts": [{"type": "text", "text": "part 2"}]}}


@pytest.mark.asyncio
async def test_chat_stream_final_payload_real_path_includes_assistant_message_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FragmentStreamClient())
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        resp = await c.post("/api/chat/stream", json={"message": "hello", "session_id": "s-stream-frag", "request_id": "r-stream-frag"})
        body = await resp.text()
        events: list[tuple[str, dict]] = []
        for chunk in body.strip().split("\n\n"):
            event_name = ""
            data = ""
            for line in chunk.splitlines():
                if line.startswith("event: "):
                    event_name = line.removeprefix("event: ")
                if line.startswith("data: "):
                    data = line.removeprefix("data: ")
            if event_name and data:
                events.append((event_name, json.loads(data)))
        final_payload = next(payload for event_name, payload in events if event_name == "final")
        done_payload = next(payload for event_name, payload in events if event_name == "done")
        assert final_payload["ok"] is True
        assert final_payload["completion_state"] == "completed"
        assert final_payload["response"]
        assert final_payload["assistant_message_ids"] == ["a-frag-1", "a-frag-2"]
        assert final_payload["assistant_message_id"] == "a-frag-2"
        assert done_payload == {"ok": True}
    finally:
        await c.close()
