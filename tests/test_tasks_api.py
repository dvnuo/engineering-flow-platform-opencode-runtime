import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.task_store import TaskRecord, utc_now_iso
from efp_opencode_adapter.tasks_api import _assistant_text_from_messages
from test_t06_helpers import FakeOpenCodeClient


class FakeTaskOpenCodeClient(FakeOpenCodeClient):
    def __init__(self, final_text=None, real_shape=False, nested_session=False, stream_events=None, no_assistant=False, prompt_result=None):
        super().__init__()
        self.prompt_async_calls = []
        self.final_text = final_text or '{"status":"success","summary":"done","artifacts":[],"blockers":[],"next_recommendation":"","audit_trace":[],"external_actions":[]}'
        self.real_shape = real_shape
        self.nested_session = nested_session
        self.stream_events = stream_events or []
        self.no_assistant = no_assistant
        self.prompt_result = {"id": "async-1"} if prompt_result is None else prompt_result

    async def create_session(self, title=None):
        self.create_calls += 1
        sid = f"ses-{self.next_id}"
        self.next_id += 1
        self.sessions[sid] = {"id": sid, "title": title or "Chat"}
        self.messages[sid] = []
        if self.nested_session:
            return {"data": {"id": sid}}
        return {"id": sid, "title": title or "Chat"}

    async def prompt_async(self, session_id, payload):
        self.prompt_async_calls.append((session_id, payload))
        self.messages[session_id].append({"id": "u", "role": "user", "parts": [{"type": "text", "text": payload['parts'][0]['text']}]})
        if not self.no_assistant:
            if self.real_shape:
                self.messages[session_id].append({"info": {"id": "a1", "role": "assistant"}, "parts": [{"type": "text", "text": self.final_text}]})
            else:
                self.messages[session_id].append({"id": "a", "role": "assistant", "parts": [{"type": "text", "text": self.final_text}]})
        return self.prompt_result

    async def event_stream(self, *, global_events=False, timeout_seconds=None):
        for event in self.stream_events:
            yield event


async def _wait_terminal(client, task_id, tries=80):
    payload = None
    for _ in range(tries):
        payload = await (await client.get(f'/api/tasks/{task_id}')).json()
        if payload['status'] in {'success', 'error', 'blocked'}:
            return payload
        await asyncio.sleep(0.02)
    return payload


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

    final = await _wait_terminal(client, 't1')
    assert final['status'] == 'success'
    assert final['output_payload']['summary'] == 'done'

    types = [(await ws.receive_json())['type'], (await ws.receive_json())['type'], (await ws.receive_json())['type']]
    assert types == ['task.accepted', 'task.started', 'task.completed']

    r2 = await client.post('/api/tasks/execute', json={'task_id': 't1', 'task_type': 'generic_agent_task', 'input_payload': {}, 'metadata': {}})
    assert r2.status == 200
    assert len(fake.prompt_async_calls) == 1
    await ws.close()
    await client.close()


@pytest.mark.asyncio
async def test_real_shape_and_prompt_id_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('EFP_TASK_COMPLETION_POLL_SECONDS', '0.01')
    fake = FakeTaskOpenCodeClient(final_text='{"status":"success","summary":"real shape"}', real_shape=True, prompt_result={'id': 'async-123'})
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app)); await c.start_server()
    await c.post('/api/tasks/execute', json={'task_id': 'tshape', 'task_type': 'generic_agent_task', 'input_payload': {}, 'metadata': {}})
    p = await _wait_terminal(c, 'tshape')
    assert p['status'] == 'success'
    assert p['output_payload']['summary'] == 'real shape'
    task_json = json.loads((tmp_path / 'state' / 'tasks' / 'tshape.json').read_text())
    assert task_json['completion_source'] == 'messages'
    assert task_json['opencode_prompt_id'] == 'async-123'
    await c.close()


@pytest.mark.asyncio
async def test_prompt_async_none_uses_generated_message_id(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('EFP_TASK_COMPLETION_POLL_SECONDS', '0.01')
    fake = FakeTaskOpenCodeClient()
    fake.prompt_result = None
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app)); await c.start_server()
    await c.post('/api/tasks/execute', json={'task_id': 'tnone', 'task_type': 'generic_agent_task', 'input_payload': {}, 'metadata': {}})
    payload = fake.prompt_async_calls[0][1]
    assert payload.get("messageID")
    task_json = json.loads((tmp_path / 'state' / 'tasks' / 'tnone.json').read_text())
    assert task_json['opencode_prompt_id'] == payload["messageID"]
    assert task_json['opencode_message_id'] == payload["messageID"]
    await c.close()


@pytest.mark.asyncio
async def test_event_stream_completion_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('EFP_TASK_COMPLETION_POLL_SECONDS', '0.01')
    fake = FakeTaskOpenCodeClient(no_assistant=True)
    fake.prompt_result = None
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app)); await c.start_server()
    await c.post('/api/tasks/execute', json={'task_id': 'tev', 'task_type': 'generic_agent_task', 'input_payload': {}, 'metadata': {}})
    sid = fake.prompt_async_calls[0][0]
    fake.stream_events = [{"type": "message.completed", "session_id": sid, "message": {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": '{"status":"success","summary":"from event"}'}]}}]
    p = await _wait_terminal(c, 'tev')
    assert p['output_payload']['summary'] == 'from event'
    task_json = json.loads((tmp_path / 'state' / 'tasks' / 'tev.json').read_text())
    assert task_json['completion_source'] == 'opencode_event'
    await c.close()


@pytest.mark.asyncio
async def test_official_global_permission_updated_timeout_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('EFP_TASK_COMPLETION_TIMEOUT_SECONDS', '0.05')
    monkeypatch.setenv('EFP_TASK_COMPLETION_POLL_SECONDS', '0.01')
    fake = FakeTaskOpenCodeClient(no_assistant=True)
    fake.prompt_result = None
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app)); await c.start_server()
    await c.post('/api/tasks/execute', json={'task_id': 'tperm', 'task_type': 'generic_agent_task', 'input_payload': {}, 'metadata': {}})
    sid = fake.prompt_async_calls[0][0]
    msg_id = fake.prompt_async_calls[0][1]["messageID"]
    fake.stream_events = [{
        "directory": "/workspace",
        "payload": {
            "type": "permission.updated",
            "properties": {"id": "perm-1", "sessionID": sid, "messageID": msg_id, "type": "tool", "metadata": {}},
        },
    }]
    p = await _wait_terminal(c, 'tperm', tries=120)
    assert p['status'] == 'blocked'
    assert p['output_payload']['error_code'] == 'permission_request_timeout'
    assert 'perm-1' in p['output_payload']['pending_permission_ids']
    await c.close()


@pytest.mark.asyncio
async def test_permission_replied_removes_pending(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('EFP_TASK_COMPLETION_TIMEOUT_SECONDS', '0.05')
    monkeypatch.setenv('EFP_TASK_COMPLETION_POLL_SECONDS', '0.01')
    fake = FakeTaskOpenCodeClient(no_assistant=True)
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app)); await c.start_server()
    await c.post('/api/tasks/execute', json={'task_id': 'treply', 'task_type': 'generic_agent_task', 'input_payload': {}, 'metadata': {}})
    sid = fake.prompt_async_calls[0][0]
    msg_id = fake.prompt_async_calls[0][1]["messageID"]
    fake.stream_events = [
        {"directory": "/workspace", "payload": {"type": "permission.updated", "properties": {"id": "perm-1", "sessionID": sid, "messageID": msg_id}}},
        {"directory": "/workspace", "payload": {"type": "permission.replied", "properties": {"permissionID": "perm-1", "sessionID": sid, "response": "allow"}}},
    ]
    p = await _wait_terminal(c, 'treply', tries=120)
    assert p['status'] == 'blocked'
    assert p['output_payload']['error_code'] == 'task_completion_timeout'
    assert not p['output_payload'].get('pending_permission_ids')
    await c.close()


@pytest.mark.asyncio
async def test_nested_create_session_response_used(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('EFP_TASK_COMPLETION_POLL_SECONDS', '0.01')
    fake = FakeTaskOpenCodeClient(nested_session=True)
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app)); await c.start_server()
    await c.post('/api/tasks/execute', json={'task_id': 'tnested', 'task_type': 'generic_agent_task', 'input_payload': {}, 'metadata': {}})
    assert fake.prompt_async_calls[0][0].startswith('ses-')
    await c.close()


@pytest.mark.asyncio
async def test_official_assistant_parent_id_matches(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('EFP_TASK_COMPLETION_POLL_SECONDS', '0.01')
    fake = FakeTaskOpenCodeClient(no_assistant=True)
    fake.prompt_result = None
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app)); await c.start_server()
    await c.post('/api/tasks/execute', json={'task_id': 'tparent', 'task_type': 'generic_agent_task', 'input_payload': {}, 'metadata': {}})
    sid = fake.prompt_async_calls[0][0]
    msg_id = fake.prompt_async_calls[0][1]["messageID"]
    fake.messages[sid].append({
        "info": {"id": "assistant-1", "sessionID": sid, "role": "assistant", "parentID": msg_id},
        "parts": [{"id": "part-1", "sessionID": sid, "messageID": "assistant-1", "type": "text", "text": '{"status":"success","summary":"matched parent"}'}],
    })
    p = await _wait_terminal(c, 'tparent')
    assert p['status'] == 'success'
    assert p['output_payload']['summary'] == 'matched parent'
    await c.close()


def test_same_session_wrong_parent_does_not_cross_match():
    rec_a = TaskRecord(task_id="a", task_type="generic_agent_task", request_id="ra", status="running", portal_session_id="pa", opencode_session_id="ses-1", input_payload={}, metadata={}, output_payload={}, artifacts={}, runtime_events=[], error=None, created_at=utc_now_iso(), opencode_message_id="msg-a", opencode_prompt_id="msg-a")
    rec_b = TaskRecord(task_id="b", task_type="generic_agent_task", request_id="rb", status="running", portal_session_id="pb", opencode_session_id="ses-1", input_payload={}, metadata={}, output_payload={}, artifacts={}, runtime_events=[], error=None, created_at=utc_now_iso(), opencode_message_id="msg-b", opencode_prompt_id="msg-b")
    messages = [{
        "info": {"id": "assistant-1", "sessionID": "ses-1", "role": "assistant", "parentID": "msg-b"},
        "parts": [{"type": "text", "text": '{"status":"success","summary":"for b"}'}],
    }]
    assert _assistant_text_from_messages(messages, 0, rec_a) is None
    assert _assistant_text_from_messages(messages, 0, rec_b) is not None


@pytest.mark.asyncio
async def test_tasks_special_cases(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('EFP_TASK_COMPLETION_POLL_SECONDS', '0.01')
    fake = FakeTaskOpenCodeClient(final_text='not-json completion')
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app)); await c.start_server()
    await c.post('/api/tasks/execute', json={'task_id': 't2', 'task_type': 'generic_agent_task', 'input_payload': {}, 'metadata': {}})
    p = await _wait_terminal(c, 't2')
    assert p['output_payload']['raw_text']

    fake2 = FakeTaskOpenCodeClient(final_text='{"status":"error","error_code":"superseded_by_new_head_sha","summary":"stale"}')
    app2 = create_app(Settings.from_env(), opencode_client=fake2)
    c2 = TestClient(TestServer(app2)); await c2.start_server()
    await c2.post('/api/tasks/execute', json={'task_id': 't3', 'task_type': 'github_review_task', 'input_payload': {}, 'metadata': {}})
    p2 = await _wait_terminal(c2, 't3')
    assert p2['output_payload']['error_code'] == 'superseded_by_new_head_sha'

    fake3 = FakeTaskOpenCodeClient(final_text='{"status":"success","summary":"delegated"}')
    app3 = create_app(Settings.from_env(), opencode_client=fake3)
    c3 = TestClient(TestServer(app3)); await c3.start_server()
    await c3.post('/api/tasks/execute', json={'task_id': 't4', 'task_type': 'delegation_task', 'input_payload': {}, 'metadata': {}})
    p3 = await _wait_terminal(c3, 't4')
    assert isinstance(p3['output_payload']['delegation_result'], dict)

    bad = await c3.post('/api/tasks/execute', data='[1]')
    assert bad.status == 400
    await c.close(); await c2.close(); await c3.close()
