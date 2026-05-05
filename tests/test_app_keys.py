from pathlib import Path
import asyncio
import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.app_keys import EVENT_BRIDGE_KEY, EVENT_BRIDGE_TASK_KEY
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings

ADAPTER_DIR = Path(__file__).resolve().parents[1] / 'efp_opencode_adapter'
BAD_SNIPPETS = ['app.get("settings"','app.get("state_paths"','app.get("session_store"','app.get("task_store"','app.get("chatlog_store"','app.get("usage_tracker"','app.get("event_bus"','app.get("task_background_tasks"','app.get("opencode_client"','app.get("portal_metadata_client"','app.get("recovery_manager"','app.get("event_bridge"','app.get("event_bridge_task"','app.get("file_service"','app.get("attachment_service"','request.app.get("settings"','request.app.get("state_paths"','request.app.get("session_store"','request.app.get("task_store"','request.app.get("chatlog_store"','request.app.get("usage_tracker"','request.app.get("event_bus"','request.app.get("task_background_tasks"','request.app.get("opencode_client"','request.app.get("portal_metadata_client"','request.app.get("recovery_manager"','request.app.get("event_bridge"','request.app.get("event_bridge_task"','request.app.get("file_service"','request.app.get("attachment_service"','app["','request.app["']

def test_adapter_does_not_use_string_aiohttp_app_keys():
    offenders=[]
    for path in ADAPTER_DIR.glob('*.py'):
        if path.name == 'app_keys.py':
            continue
        text = path.read_text(encoding='utf-8')
        for snippet in BAD_SNIPPETS:
            if snippet in text:
                offenders.append(f'{path.name}: {snippet}')
    assert not offenders

def test_adapter_does_not_wildcard_import_app_keys():
    offenders=[]
    for path in ADAPTER_DIR.glob('*.py'):
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
