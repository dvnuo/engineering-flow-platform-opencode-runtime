import os
import pytest


def test_chat_contract_when_enabled(post_json):
    if os.getenv('RUNTIME_CONTRACT_ENABLE_CHAT') != '1':
        pytest.skip('chat contract disabled')
    status, body = post_json('/api/chat', {'message': 'hello', 'session_id': 'contract-chat'})
    assert status == 200 and ('response' in body or 'error' in body)


def test_task_contract_when_enabled(post_json, get_json):
    if os.getenv('RUNTIME_CONTRACT_ENABLE_TASKS') != '1':
        pytest.skip('task contract disabled')
    status, body = post_json('/api/tasks/execute', {'task_type': 'generic', 'task_input': {'goal': 'ping'}, 'portal_session_id': 'contract-task'})
    assert status == 202
    _, detail = get_json(f"/api/tasks/{body['task_id']}")
    assert detail['status'] in {'accepted', 'running', 'success', 'error', 'blocked'}
