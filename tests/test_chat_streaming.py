from efp_opencode_adapter.app_keys import EVENT_BUS_KEY, OPENCODE_CLIENT_KEY
import asyncio
import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.opencode_client import OpenCodeClientError
from efp_opencode_adapter.chat_api import SSEClientDisconnected, _safe_write_eof, _write_sse
from test_t06_helpers import FakeOpenCodeClient

class SlowFake(FakeOpenCodeClient):
    def __init__(self, fail=False):
        super().__init__(); self.entered=asyncio.Event(); self.release=asyncio.Event(); self.fail=fail
    async def send_message(self, session_id, **kwargs):
        self.entered.set(); await self.release.wait()
        if self.fail: raise OpenCodeClientError('boom')
        return {"messages":[{"role":"assistant","content":"ok"}]}

@pytest.mark.asyncio
async def test_chat_stream_forwards_runtime_event_before_final(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    fake=SlowFake(); app=create_app(Settings.from_env(), opencode_client=fake); c=TestClient(TestServer(app)); await c.start_server()
    t=asyncio.create_task(c.post('/api/chat/stream', json={'message':'m','session_id':'portal-stream-1','request_id':'req-stream-1'}))
    await fake.entered.wait()
    await app[EVENT_BUS_KEY].publish({'type':'tool.started','session_id':'portal-stream-1','request_id':'raw-opencode-tool-call-id-not-portal-request','tool':'efp_test_tool','engine':'opencode','raw_type':'tool.start'})
    fake.release.set(); resp=await t; body=await resp.text()
    assert body.index('tool.started') < body.index('event: final')
    await c.close()

@pytest.mark.asyncio
async def test_chat_stream_sends_delta_event_for_assistant_delta(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    fake=SlowFake(); app=create_app(Settings.from_env(), opencode_client=fake); c=TestClient(TestServer(app)); await c.start_server()
    t=asyncio.create_task(c.post('/api/chat/stream', json={'message':'m','session_id':'portal-stream-2','request_id':'req-stream-2'}))
    await fake.entered.wait()
    await app[EVENT_BUS_KEY].publish({'type':'assistant_delta','session_id':'portal-stream-2','request_id':'raw-message-part-id','data':{'delta':'hello delta'},'engine':'opencode','raw_type':'message.part.updated'})
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
    async def send_message(self, payload, **kwargs):
        await self._bus.publish({'type':'tool.completed','session_id':'portal-race-1','request_id':'raw-opencode-tool-call-id','tool':'efp_race_tool','raw_type':'tool.complete'})
        return {'ok': True, 'session_id': payload.get('session_id'), 'request_id': payload.get('request_id')}


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
        await app[EVENT_BUS_KEY].publish({'type': 'assistant_delta', 'session_id': 'portal-disconnect-1', 'request_id': 'raw-message-part-id', 'data': {'delta': 'hello'}})
        fake.release.set()
        resp = await t
        assert resp.status == 200
        await resp.release()
        await asyncio.wait_for(fake.finished.wait(), timeout=1.0)
        assert fake.was_cancelled is False
        assert len(app[EVENT_BUS_KEY]._subs) == 0
    finally:
        monkeypatch.setattr(chat_api, "_write_sse", original_write_sse)
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
        {'type':'message.delta','session_id':'portal-stream-filter-1','request_id':'raw-id','raw_type':'message.part.updated','data':{'delta':'Hi'}},
        {'type':'llm_thinking','session_id':'portal-stream-filter-1','request_id':'raw-id','data':{'message':'thinking'}},
    ]:
        await app[EVENT_BUS_KEY].publish(evt)
    fake.release.set(); resp=await t; body=await resp.text()
    assert 'opencode.sync' not in body
    assert 'session.updated' not in body
    assert 'opencode.step.finished' not in body
    assert 'unknown.debug' not in body
    assert 'event: delta' in body and 'Hi' in body
    assert 'llm_thinking' in body
    await c.close()


@pytest.mark.asyncio
async def test_chat_stream_does_not_duplicate_real_and_synthetic_delta(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    fake=SlowFake(); app=create_app(Settings.from_env(), opencode_client=fake); c=TestClient(TestServer(app)); await c.start_server()
    t=asyncio.create_task(c.post('/api/chat/stream', json={'message':'m','session_id':'portal-stream-dup-1','request_id':'req-stream-dup-1'}))
    await fake.entered.wait()
    await app[EVENT_BUS_KEY].publish({'type':'message.delta','session_id':'portal-stream-dup-1','request_id':'raw-1','raw_type':'message.part.updated','data':{'delta':'Hi'}})
    await app[EVENT_BUS_KEY].publish({'type':'assistant_delta','session_id':'portal-stream-dup-1','request_id':'req-stream-dup-1','synthetic_final_delta':True,'data':{'delta':'Hi','synthetic_final_delta':True}})
    fake.release.set(); resp=await t; body=await resp.text()
    assert body.count('event: delta') == 1
    await c.close()
