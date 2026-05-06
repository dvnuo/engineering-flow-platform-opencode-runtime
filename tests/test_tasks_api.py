import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.task_store import TaskRecord, utc_now_iso
from efp_opencode_adapter.tasks_api import _assistant_text_from_event, _assistant_text_from_messages, _permission_event_delta, cleanup_task_background_tasks
from test_t06_helpers import FakeOpenCodeClient


class FakeTaskOpenCodeClient(FakeOpenCodeClient):
    def __init__(self, final_text=None, real_shape=False, nested_session=False, stream_events=None, no_assistant=False, prompt_result=None, raise_prompt_async=False):
        super().__init__()
        self.prompt_async_calls = []
        self.final_text = final_text or '{"status":"success","summary":"done","artifacts":[],"blockers":[],"next_recommendation":"","audit_trace":[],"external_actions":[]}'
        self.real_shape = real_shape
        self.nested_session = nested_session
        self.stream_events = stream_events or []
        self.no_assistant = no_assistant
        self.prompt_result = {"id": "async-1"} if prompt_result is None else prompt_result
        self.message_details: dict[tuple[str, str], dict] = {}
        self.raise_prompt_async = raise_prompt_async
        self.cancel_calls = []

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
        if self.raise_prompt_async:
            raise RuntimeError("prompt_async failure")
        self.prompt_async_calls.append((session_id, payload))
        self.messages[session_id].append({"id": "u", "role": "user", "parts": [{"type": "text", "text": payload['parts'][0]['text']}]})
        if not self.no_assistant:
            if self.real_shape:
                self.messages[session_id].append({"info": {"id": "a1", "role": "assistant"}, "parts": [{"type": "text", "text": self.final_text}]})
            else:
                self.messages[session_id].append({"id": "a", "role": "assistant", "parts": [{"type": "text", "text": self.final_text}]})
        return self.prompt_result

    async def get_message(self, session_id, message_id):
        return self.message_details.get((session_id, message_id), {})

    async def event_stream(self, *, global_events=False, timeout_seconds=None):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(timeout_seconds or 0)
        emitted = 0
        while True:
            while emitted < len(self.stream_events):
                event = self.stream_events[emitted]
                emitted += 1
                yield event
            if timeout_seconds is None or loop.time() >= deadline:
                break
            await asyncio.sleep(0.005)

    async def cancel_message(self, session_id, message_id=None):
        self.cancel_calls.append((session_id, message_id))
        return {"success": False, "supported": False, "reason": "cancel_endpoint_unsupported"}


async def _wait_terminal(client, task_id, tries=80):
    payload = None
    for _ in range(tries):
        payload = await (await client.get(f'/api/tasks/{task_id}')).json()
        if payload['status'] in {'success', 'error', 'blocked'}:
            return payload
        await asyncio.sleep(0.02)
    return payload


@pytest.mark.asyncio
async def test_task_events_include_trace_context_and_group_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('EFP_TASK_COMPLETION_POLL_SECONDS', '0.01')
    monkeypatch.setenv('PORTAL_AGENT_ID', 'agent-task-1')
    fake = FakeTaskOpenCodeClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()
    ws = await client.ws_connect('/api/events?task_id=t-obs-1')
    await ws.receive_json()
    r = await client.post('/api/tasks/execute', json={'task_id': 't-obs-1', 'task_type': 'generic_agent_task', 'request_id': 'req-task-1', 'session_id': 'sess-task-1', 'input_payload': {}, 'metadata': {'group_id': 'group-1', 'coordination_run_id': 'coord-1', 'runtime_profile_id': 'rp-task-1', 'runtime_profile': {'revision': 3}}})
    assert r.status == 202
    events = [await ws.receive_json(), await ws.receive_json(), await ws.receive_json()]
    target = next(e for e in events if e['type'] in {'task.accepted', 'task.started', 'task.completed'})
    tc = target['trace_context']
    assert tc['agent_id'] == 'agent-task-1'
    assert tc['runtime_type'] == 'opencode'
    assert tc['task_id'] == 't-obs-1'
    assert tc['request_id'] == 'req-task-1'
    assert tc['group_id'] == 'group-1'
    assert tc['coordination_run_id'] == 'coord-1'
    assert tc['profile_version'] == '3'
    assert tc['runtime_profile_id'] == 'rp-task-1'
    assert target['data']['trace_context']
    assert target['group_id'] == 'group-1'
    assert target['coordination_run_id'] == 'coord-1'
    await ws.close(); await client.close()


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
    msg_id = fake.prompt_async_calls[0][1]["messageID"]
    fake.stream_events = [{"type": "message.completed", "session_id": sid, "message": {"info": {"role": "assistant", "parentID": msg_id}, "parts": [{"type": "text", "text": '{"status":"success","summary":"from event"}'}]}}]
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
async def test_official_permission_asked_timeout_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('EFP_TASK_COMPLETION_TIMEOUT_SECONDS', '0.05')
    monkeypatch.setenv('EFP_TASK_COMPLETION_POLL_SECONDS', '0.01')
    fake = FakeTaskOpenCodeClient(no_assistant=True)
    fake.prompt_result = None
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app)); await c.start_server()
    await c.post('/api/tasks/execute', json={'task_id': 'taskedperm', 'task_type': 'generic_agent_task', 'input_payload': {}, 'metadata': {}})
    sid = fake.prompt_async_calls[0][0]
    fake.stream_events = [{"directory": "/workspace", "payload": {"type": "permission.asked", "properties": {"requestID": "perm-asked-1", "sessionID": sid}}}]
    p = await _wait_terminal(c, 'taskedperm', tries=120)
    assert p['status'] == 'blocked'
    assert p['output_payload']['error_code'] == 'permission_request_timeout'
    assert 'perm-asked-1' in p['output_payload']['pending_permission_ids']
    await c.close()


def test_permission_updated_approved_resolves_pending():
    event = {"payload": {"type": "permission.updated", "properties": {"id": "perm-1", "status": "approved"}}}
    assert _permission_event_delta(event) == ("resolved", "perm-1")


def test_permission_updated_denied_resolves_pending():
    event = {"payload": {"type": "permission.updated", "properties": {"id": "perm-1", "status": "denied"}}}
    assert _permission_event_delta(event) == ("resolved", "perm-1")


def test_permission_updated_without_status_remains_open():
    event = {"payload": {"type": "permission.updated", "properties": {"id": "perm-1"}}}
    assert _permission_event_delta(event) == ("open", "perm-1")


def test_permission_updated_response_allow_resolves_pending():
    event = {"payload": {"type": "permission.updated", "properties": {"id": "perm-1", "response": "allow"}}}
    assert _permission_event_delta(event) == ("resolved", "perm-1")


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


@pytest.mark.asyncio
async def test_prompt_result_id_does_not_replace_generated_message_id_parent_correlation(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('EFP_TASK_COMPLETION_POLL_SECONDS', '0.01')
    fake = FakeTaskOpenCodeClient(no_assistant=True, prompt_result={"id": "async-123"})
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app)); await c.start_server()
    await c.post('/api/tasks/execute', json={'task_id': 'tcorrelate', 'task_type': 'generic_agent_task', 'input_payload': {}, 'metadata': {}})
    sid = fake.prompt_async_calls[0][0]
    generated = fake.prompt_async_calls[0][1]["messageID"]
    fake.messages[sid].append({
        "info": {"id": "assistant-1", "sessionID": sid, "role": "assistant", "parentID": generated},
        "parts": [{"type": "text", "text": '{"status":"success","summary":"parent generated"}'}],
    })
    p = await _wait_terminal(c, 'tcorrelate')
    assert p["status"] == "success"
    assert p["output_payload"]["summary"] == "parent generated"
    task_json = json.loads((tmp_path / 'state' / 'tasks' / 'tcorrelate.json').read_text())
    assert task_json["opencode_prompt_id"] == "async-123"
    assert task_json["opencode_message_id"] == generated
    await c.close()


@pytest.mark.asyncio
async def test_official_message_part_updated_fetches_full_message(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('EFP_TASK_COMPLETION_POLL_SECONDS', '0.01')
    fake = FakeTaskOpenCodeClient(no_assistant=True)
    fake.prompt_result = None
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app)); await c.start_server()
    try:
        await c.post('/api/tasks/execute', json={'task_id': 'tpart', 'task_type': 'generic_agent_task', 'input_payload': {}, 'metadata': {}})
        sid = fake.prompt_async_calls[0][0]
        user_msg_id = fake.prompt_async_calls[0][1]['messageID']
        fake.message_details[(sid, 'assistant-1')] = {
            "info": {"id": "assistant-1", "sessionID": sid, "role": "assistant", "parentID": user_msg_id},
            "parts": [{"type": "text", "text": '{"status":"success","summary":"from official part"}'}],
        }
        fake.stream_events = [{"directory": "/workspace", "payload": {"type": "message.part.updated", "properties": {"part": {"sessionID": sid, "messageID": "assistant-1", "type": "text", "text": '{"status":"success","summary":"partial"}'}}}}]
        p = await _wait_terminal(c, 'tpart')
        assert p['status'] == 'success'
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_cancel_running_task_marks_cancelled_and_publishes_events(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('EFP_TASK_COMPLETION_TIMEOUT_SECONDS', '5')
    monkeypatch.setenv('EFP_TASK_COMPLETION_POLL_SECONDS', '0.01')
    fake = FakeTaskOpenCodeClient(no_assistant=True)
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app)); await c.start_server()
    await c.post('/api/tasks/execute', json={'task_id': 'tcancel', 'task_type': 'generic_agent_task', 'input_payload': {}, 'metadata': {}})
    await asyncio.sleep(0.05)
    body = await (await c.post('/api/tasks/tcancel/cancel')).json()
    assert body['status'] == 'cancelled' and body['ok'] is False and body['output_payload']['error_code'] == 'cancelled'
    assert fake.cancel_calls
    got = await (await c.get('/api/tasks/tcancel')).json()
    types = [e.get('type') for e in got.get('runtime_events', [])]
    assert 'task.cancelled' in types and 'task.completed' in types and got['status'] == 'cancelled'
    await c.close()


@pytest.mark.asyncio
async def test_cancel_terminal_task_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('EFP_TASK_COMPLETION_POLL_SECONDS', '0.01')
    fake = FakeTaskOpenCodeClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app)); await c.start_server()
    await c.post('/api/tasks/execute', json={'task_id': 'tidem', 'task_type': 'generic_agent_task', 'input_payload': {}, 'metadata': {}})
    payload = await _wait_terminal(c, 'tidem')
    assert payload['status'] == 'success'
    fake.cancel_calls = []
    cancelled = await (await c.post('/api/tasks/tidem/cancel')).json()
    assert cancelled['status'] == 'success'
    assert fake.cancel_calls == []
    await c.close()


def test_message_part_updated_text_is_not_final_completion():
    evt = {
        "type": "message.part.updated",
        "properties": {
            "part": {
                "sessionID": "ses-1",
                "messageID": "assistant-1",
                "type": "text",
                "text": '{"status":"success","summary":"partial"}',
            }
        },
    }
    assert _assistant_text_from_event(evt) is None


@pytest.mark.asyncio
async def test_message_part_wrong_parent_does_not_complete_task(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('EFP_TASK_COMPLETION_TIMEOUT_SECONDS', '0.05')
    monkeypatch.setenv('EFP_TASK_COMPLETION_POLL_SECONDS', '0.01')
    fake = FakeTaskOpenCodeClient(no_assistant=True)
    fake.prompt_result = None
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app)); await c.start_server()
    await c.post('/api/tasks/execute', json={'task_id': 'twrongparent', 'task_type': 'generic_agent_task', 'input_payload': {}, 'metadata': {}})
    sid = fake.prompt_async_calls[0][0]
    fake.message_details[(sid, 'assistant-other')] = {
        "info": {"id": "assistant-other", "sessionID": sid, "role": "assistant", "parentID": "some-other-user-message"},
        "parts": [{"type": "text", "text": '{"status":"success","summary":"wrong task"}'}],
    }
    fake.stream_events = [{"directory": "/workspace", "payload": {"type": "message.part.updated", "properties": {"part": {"sessionID": sid, "messageID": "assistant-other", "type": "text", "text": '{"status":"success","summary":"partial"}'}}}}]
    p = await _wait_terminal(c, 'twrongparent', tries=120)
    assert p['status'] == 'blocked'
    assert p['output_payload']['error_code'] == 'task_completion_timeout'
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
async def test_prompt_async_generic_exception_marks_error_and_preserves_events(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    fake = FakeTaskOpenCodeClient(raise_prompt_async=True)
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app)); await c.start_server()
    r = await c.post('/api/tasks/execute', json={'task_id': 'tfaildispatch', 'task_type': 'generic_agent_task', 'input_payload': {}, 'metadata': {}})
    assert r.status == 502
    p = await (await c.get('/api/tasks/tfaildispatch')).json()
    assert p['status'] == 'error'
    assert p['output_payload']['error_code'] == 'opencode_error'
    event_types = [e['type'] for e in p['runtime_events']]
    assert 'task.accepted' in event_types
    assert 'task.completed' in event_types
    await c.close()


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

@pytest.mark.asyncio
async def test_cleanup_task_background_tasks_uses_appkey_and_cancels_tasks():
    from aiohttp import web
    from efp_opencode_adapter.app_keys import TASK_BACKGROUND_TASKS_KEY
    app = web.Application()
    sleeper = asyncio.create_task(asyncio.sleep(60))
    app[TASK_BACKGROUND_TASKS_KEY] = {sleeper}
    await cleanup_task_background_tasks(app)
    assert sleeper.done()
    assert app[TASK_BACKGROUND_TASKS_KEY] == set()

@pytest.mark.asyncio
async def test_testclient_close_cancels_pending_task_collectors(tmp_path, monkeypatch):
    from efp_opencode_adapter.app_keys import TASK_BACKGROUND_TASKS_KEY
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('EFP_TASK_COMPLETION_TIMEOUT_SECONDS', '60')
    monkeypatch.setenv('EFP_TASK_COMPLETION_POLL_SECONDS', '0.1')
    fake = FakeTaskOpenCodeClient(no_assistant=True)
    app = create_app(Settings.from_env(), opencode_client=fake)
    c = TestClient(TestServer(app))
    await c.start_server()
    await c.post('/api/tasks/execute', json={'task_id': 'tleak', 'task_type': 'generic_agent_task', 'input_payload': {}, 'metadata': {}})
    await asyncio.sleep(0.05)
    tasks = list(app[TASK_BACKGROUND_TASKS_KEY])
    assert tasks and any(not task.done() for task in tasks)
    await c.close()
    await asyncio.sleep(0)
    assert all(task.done() for task in tasks)
    assert app[TASK_BACKGROUND_TASKS_KEY] == set()
