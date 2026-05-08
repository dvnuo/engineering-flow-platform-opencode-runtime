import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


class TrackingPermissionClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.permission_calls = []

    async def respond_permission(self, session_id, permission_id, payload):
        self.permission_calls.append((session_id, permission_id, payload))
        return {"success": True}


@pytest.mark.asyncio
async def test_permission_respond_has_trace_context_and_request_id(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('PORTAL_AGENT_ID', 'agent-perm-1')
    fake = TrackingPermissionClient()
    fake.sessions['ses-1'] = {'id': 'ses-1', 'title': 'x'}
    fake.messages['ses-1'] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app)); await client.start_server()

    ws = await client.ws_connect('/api/events?session_id=sess-perm-1')
    await ws.receive_json()
    body = {"decision": "allow", "remember": True, "session_id": "sess-perm-1", "opencode_session_id": "ses-1", "request_id": "req-perm-1", "tool": "bash"}
    resp = await client.post('/api/permissions/perm-1/respond', json=body)
    assert resp.status == 200
    assert fake.permission_calls == [('ses-1', 'perm-1', {'response': 'always'})]

    event = await ws.receive_json(timeout=2)
    assert event['type'] == 'permission_resolved'
    tc = event['trace_context']
    assert tc['agent_id'] == 'agent-perm-1'
    assert tc['request_id'] == 'req-perm-1'
    assert tc['session_id'] == 'sess-perm-1'
    assert tc['opencode_session_id'] == 'ses-1'
    assert tc['tool_name'] == 'bash'
    assert event['data']['trace_context']

    resp2 = await client.post('/api/permissions/perm-2/respond', json={**body, 'request_id': 'token-permission-secret'})
    assert resp2.status == 200
    event2 = await ws.receive_json(timeout=2)
    assert 'token-permission-secret' not in json.dumps(event2).lower()
    await ws.close(); await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"decision": "allow", "remember": False}, {"response": "once"}),
        ({"decision": "allow", "remember": True}, {"response": "always"}),
        ({"decision": "approve", "remember": True}, {"response": "always"}),
        ({"decision": "deny"}, {"response": "reject"}),
        ({"decision": "reject"}, {"response": "reject"}),
        ({"response": "once", "decision": "allow"}, {"response": "once"}),
        ({"response": "always", "decision": "deny"}, {"response": "always"}),
        ({"response": "reject", "decision": "allow"}, {"response": "reject"}),
    ],
)
async def test_permission_respond_maps_body_to_opencode_response(tmp_path, monkeypatch, body, expected):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = TrackingPermissionClient()
    fake.sessions["ses-1"] = {"id": "ses-1", "title": "x"}
    fake.messages["ses-1"] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()
    payload = {"session_id": "sess-perm-1", "opencode_session_id": "ses-1", "request_id": "req-perm-1", **body}
    resp = await client.post("/api/permissions/perm-1/respond", json=payload)
    assert resp.status == 200
    assert fake.permission_calls[-1] == ("ses-1", "perm-1", expected)
    await client.close()
