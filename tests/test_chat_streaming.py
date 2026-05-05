import asyncio
import pytest
from aiohttp.test_utils import TestClient, TestServer
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.opencode_client import OpenCodeClientError
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
    await app['event_bus'].publish({'type':'tool.started','session_id':'portal-stream-1','request_id':'raw-opencode-tool-call-id-not-portal-request','tool':'efp_test_tool','engine':'opencode','raw_type':'tool.start'})
    fake.release.set(); resp=await t; body=await resp.text()
    assert body.index('tool.started') < body.index('event: final')
    await c.close()

@pytest.mark.asyncio
async def test_chat_stream_sends_delta_event_for_assistant_delta(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    fake=SlowFake(); app=create_app(Settings.from_env(), opencode_client=fake); c=TestClient(TestServer(app)); await c.start_server()
    t=asyncio.create_task(c.post('/api/chat/stream', json={'message':'m','session_id':'portal-stream-2','request_id':'req-stream-2'}))
    await fake.entered.wait()
    await app['event_bus'].publish({'type':'assistant_delta','session_id':'portal-stream-2','request_id':'raw-message-part-id','data':{'delta':'hello delta'},'engine':'opencode','raw_type':'message.part.updated'})
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
    assert len(app['event_bus']._subs) == 0
    await c.close()


@pytest.mark.asyncio
async def test_chat_stream_respects_explicit_portal_request_id(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    fake=SlowFake(); app=create_app(Settings.from_env(), opencode_client=fake); c=TestClient(TestServer(app)); await c.start_server()
    t=asyncio.create_task(c.post('/api/chat/stream', json={'message':'m','session_id':'portal-stream-3','request_id':'req-stream-3'}))
    await fake.entered.wait()
    await app['event_bus'].publish({'type':'assistant_delta','session_id':'portal-stream-3','portal_request_id':'other-request','data':{'delta':'should not appear'}})
    fake.release.set(); resp=await t; body=await resp.text()
    assert 'should not appear' not in body
    await c.close()
