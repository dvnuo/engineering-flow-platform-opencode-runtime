from pathlib import Path
import asyncio
import ast
import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.app_keys import EVENT_BRIDGE_KEY, EVENT_BRIDGE_TASK_KEY
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings

ADAPTER_DIR = Path(__file__).resolve().parents[1] / 'efp_opencode_adapter'

STRING_APP_KEY_NAMES = {
    'settings', 'state_paths', 'session_store', 'task_store', 'chatlog_store',
    'usage_tracker', 'event_bus', 'task_background_tasks', 'opencode_client',
    'portal_metadata_client', 'recovery_manager', 'event_bridge', 'event_bridge_task',
    'file_service', 'attachment_service'
}

def test_adapter_does_not_use_string_aiohttp_app_keys():
    offenders = []
    for path in ADAPTER_DIR.rglob('*.py'):
        if path.name == 'app_keys.py':
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr != 'get':
                continue
            target = node.func.value
            is_app_get = isinstance(target, ast.Name) and target.id == 'app'
            is_request_app_get = (
                isinstance(target, ast.Attribute)
                and target.attr == 'app'
                and isinstance(target.value, ast.Name)
                and target.value.id == 'request'
            )
            if not (is_app_get or is_request_app_get) or not node.args:
                continue
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str) and first_arg.value in STRING_APP_KEY_NAMES:
                offenders.append(f"{path.name}:{node.lineno} uses string key via .get('{first_arg.value}')")


        for node in ast.walk(tree):
            if not isinstance(node, ast.Subscript):
                continue
            target = node.value
            is_app_subscript = isinstance(target, ast.Name) and target.id == 'app'
            is_request_app_subscript = (
                isinstance(target, ast.Attribute)
                and target.attr == 'app'
                and isinstance(target.value, ast.Name)
                and target.value.id == 'request'
            )
            if not (is_app_subscript or is_request_app_subscript):
                continue
            key_node = node.slice
            if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                offenders.append(f"{path.name}:{node.lineno} uses string key via subscript '{key_node.value}'")

    assert not offenders

def test_adapter_does_not_wildcard_import_app_keys():
    offenders=[]
    for path in ADAPTER_DIR.rglob('*.py'):
        if 'from .app_keys import *' in path.read_text(encoding='utf-8'):
            offenders.append(path.name)
    assert not offenders


class IdleEventStreamClient:
    async def health(self):
        return {'healthy': True, 'version': '1.14.29'}

    async def event_stream(self, global_events=True, timeout_seconds=None):
        while True:
            await asyncio.sleep(1)
            if False:
                yield {}


@pytest.mark.asyncio
async def test_event_bridge_starts_and_cleans_up_with_appkeys(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('EFP_WORKSPACE_DIR', str(tmp_path / 'workspace'))
    monkeypatch.setenv('EFP_EVENT_BRIDGE_ENABLED', 'true')
    app = create_app(Settings.from_env(), opencode_client=IdleEventStreamClient(), start_event_bridge=True)
    client = TestClient(TestServer(app))
    await client.start_server()
    task = None
    try:
        await asyncio.sleep(0.05)
        assert app.get(EVENT_BRIDGE_KEY) is not None
        task = app.get(EVENT_BRIDGE_TASK_KEY)
        assert task is not None and not task.done()
        health = await (await client.get('/health')).json()
        assert health['event_bridge']['enabled'] is True
        assert health['event_bridge']['running'] is True
    finally:
        task = app.get(EVENT_BRIDGE_TASK_KEY)
        await client.close()
    assert task is not None and task.done()
