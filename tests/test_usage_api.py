import pytest
from aiohttp.test_utils import TestClient, TestServer
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient

@pytest.mark.asyncio
async def test_usage_api(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app)); await client.start_server()
    r = await client.get('/api/usage'); assert r.status==200
    p = await r.json(); assert p['global']['total_requests']==0
    await client.post('/api/chat', json={'message':'hello'})
    p2 = await (await client.get('/api/usage?days=30')).json()
    assert p2['global']['total_requests'] >= 1
    assert p2['global']['total_messages'] >= 2
    assert (tmp_path/'state'/'usage.jsonl').exists()
    assert p2['global']['total_input_tokens'] >= 10
    p3 = await (await client.get('/api/usage?days=bad')).json(); assert p3['period_days']==30
    await client.close()
