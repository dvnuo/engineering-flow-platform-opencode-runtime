import json
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from efp_opencode_adapter.portal_metadata_client import PortalMetadataClient
from efp_opencode_adapter.settings import Settings

@pytest.mark.asyncio
async def test_portal_metadata_client(tmp_path, monkeypatch):
    monkeypatch.delenv('PORTAL_INTERNAL_BASE_URL', raising=False)
    monkeypatch.delenv('PORTAL_AGENT_ID', raising=False)
    c = PortalMetadataClient(Settings.from_env(), pending_file=tmp_path/'p.jsonl')
    r = await c.publish_session_metadata(session_id='s', latest_event_type='x', latest_event_state='y')
    assert r['skipped']

@pytest.mark.asyncio
async def test_portal_metadata_put(tmp_path, monkeypatch):
    got = {}
    async def h(req):
        got['path'] = req.path
        got['body'] = await req.json()
        return web.json_response({'ok':True})
    app = web.Application(); app.router.add_put('/api/internal/agents/agent-1/sessions/session-1/metadata', h)
    srv = TestServer(app); await srv.start_server()
    monkeypatch.setenv('PORTAL_INTERNAL_BASE_URL', str(srv.make_url('')).rstrip('/'))
    monkeypatch.setenv('PORTAL_AGENT_ID', 'agent-1')
    monkeypatch.setenv('PORTAL_INTERNAL_TOKEN', 'tok')
    c = PortalMetadataClient(Settings.from_env(), pending_file=tmp_path/'p.jsonl')
    r = await c.publish_session_metadata(session_id='session-1', latest_event_type='a', latest_event_state='b', request_id='r1', runtime_events=[{'type':'x'}], metadata={'k':'v'})
    assert r['success'] is True
    assert got['path'] == '/api/internal/agents/agent-1/sessions/session-1/metadata'
    assert isinstance(got['body']['runtime_events_json'], str)
    assert isinstance(got['body']['metadata_json'], str)
    await srv.close()
