import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


class FakeTaskOpenCodeClient(FakeOpenCodeClient):
    def __init__(self, final_text=None):
        super().__init__()
        self.prompt_async_calls = []
        self.final_text = final_text or '{"status":"success","summary":"done","artifacts":[],"blockers":[],"next_recommendation":"","audit_trace":[],"external_actions":[]}'

    async def prompt_async(self, session_id, payload):
        self.prompt_async_calls.append((session_id, payload))
        self.messages[session_id].append({"id": "u", "role": "user", "parts": [{"type": "text", "text": payload['parts'][0]['text']}]})
        self.messages[session_id].append({"id": "a", "role": "assistant", "parts": [{"type": "text", "text": self.final_text}]})
        return {"id": "async-1"}


@pytest.mark.asyncio
async def test_tasks_execute_get_events_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('EFP_TASK_COMPLETION_TIMEOUT_SECONDS', '2')
    monkeypatch.setenv('EFP_TASK_COMPLETION_POLL_SECONDS', '0.01')
    fake = FakeTaskOpenCodeClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()

    ws = await client.ws_connect('/api/events?task_id=t1')
    await ws.receive_json()
    r = await client.post('/api/tasks/execute', json={'task_id': 't1', 'task_type': 'generic_agent_task', 'input_payload': {}, 'metadata': {}})
    assert r.status == 202
    assert (await r.json())['status'] == 'accepted'
    assert (tmp_path / 'state' / 'tasks' / 't1.json').exists()

    final = None
    for _ in range(50):
        rg = await client.get('/api/tasks/t1')
        final = await rg.json()
        if final['status'] in {'success', 'error', 'blocked'}:
            break
        await asyncio.sleep(0.02)
    assert final['status'] == 'success'
    assert final['output_payload']['summary'] == 'done'

    types = [
        (await ws.receive_json())['type'],
        (await ws.receive_json())['type'],
        (await ws.receive_json())['type'],
    ]
    assert types == ['task.accepted', 'task.started', 'task.completed']

    r2 = await client.post('/api/tasks/execute', json={'task_id': 't1', 'task_type': 'generic_agent_task', 'input_payload': {}, 'metadata': {}})
    assert r2.status == 200
    assert len(fake.prompt_async_calls) == 1
    await ws.close()
    await client.close()


@pytest.mark.asyncio
async def test_tasks_special_cases(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('EFP_TASK_COMPLETION_POLL_SECONDS', '0.01')
    fake = FakeTaskOpenCodeClient(final_text='not-json completion')
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app)); await c.start_server()
    await c.post('/api/tasks/execute', json={'task_id': 't2', 'task_type': 'generic_agent_task', 'input_payload': {}, 'metadata': {}})
    for _ in range(40):
        p = await (await c.get('/api/tasks/t2')).json()
        if p['status'] == 'success':
            break
        await asyncio.sleep(0.02)
    assert p['output_payload']['raw_text']

    fake2 = FakeTaskOpenCodeClient(final_text='{"status":"error","error_code":"superseded_by_new_head_sha","summary":"stale"}')
    app2 = create_app(Settings.from_env(), opencode_client=fake2)
    c2 = TestClient(TestServer(app2)); await c2.start_server()
    await c2.post('/api/tasks/execute', json={'task_id': 't3', 'task_type': 'github_review_task', 'input_payload': {}, 'metadata': {}})
    for _ in range(40):
        p2 = await (await c2.get('/api/tasks/t3')).json()
        if p2['status'] in {'success','error','blocked'}:
            break
        await asyncio.sleep(0.02)
    assert p2['output_payload']['error_code'] == 'superseded_by_new_head_sha'

    fake3 = FakeTaskOpenCodeClient(final_text='{"status":"success","summary":"delegated"}')
    app3 = create_app(Settings.from_env(), opencode_client=fake3)
    c3 = TestClient(TestServer(app3)); await c3.start_server()
    await c3.post('/api/tasks/execute', json={'task_id': 't4', 'task_type': 'delegation_task', 'input_payload': {}, 'metadata': {}})
    for _ in range(40):
        p3 = await (await c3.get('/api/tasks/t4')).json()
        if p3['status'] == 'success':
            break
        await asyncio.sleep(0.02)
    assert isinstance(p3['output_payload']['delegation_result'], dict)

    bad = await c3.post('/api/tasks/execute', data='[1]')
    assert bad.status == 400
    await c.close(); await c2.close(); await c3.close()
