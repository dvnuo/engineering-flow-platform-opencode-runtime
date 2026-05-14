import asyncio
import pytest

from efp_opencode_adapter.event_bridge import OpenCodeEventBridge
from efp_opencode_adapter.event_bus import EventBus
from efp_opencode_adapter.request_bindings import RequestBindingStore
from efp_opencode_adapter.session_store import SessionStore
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.state import ensure_state_dirs
from efp_opencode_adapter.task_store import TaskStore


class FakeClient:
    async def event_stream(self, **kwargs):
        if False:
            yield {}


@pytest.mark.asyncio
async def test_request_binding_injected_into_event(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('EFP_WORKSPACE_DIR', str(tmp_path / 'workspace'))
    settings = Settings.from_env()
    paths = ensure_state_dirs(settings)
    bus = EventBus()
    bindings = RequestBindingStore()
    bindings.bind_message('oc-1', 'm-1', 'portal-1', 'req-1')
    bridge = OpenCodeEventBridge(settings, FakeClient(), bus, SessionStore(paths.sessions_dir), TaskStore(paths.tasks_dir), request_binding_store=bindings)
    q = bus.subscribe({'session_id': 'portal-1'})
    event = await bridge.publish_raw_event({'type': 'message.part.delta', 'sessionID': 'oc-1', 'properties': {'sessionID': 'oc-1', 'messageID': 'm-1', 'partID': 'p1', 'delta': 'hello'}})
    got = await asyncio.wait_for(q.queue.get(), timeout=1)
    assert event['session_id'] == 'portal-1'
    assert event['request_id'] == 'req-1'
    assert got['request_id'] == 'req-1'

@pytest.mark.asyncio
async def test_event_bridge_injects_opencode_session_in_data(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('EFP_WORKSPACE_DIR', str(tmp_path / 'workspace'))
    settings = Settings.from_env(); paths = ensure_state_dirs(settings)
    bus = EventBus(); bindings = RequestBindingStore()
    bindings.bind_message('oc-2', 'm-2', 'portal-2', 'req-2')
    bridge = OpenCodeEventBridge(settings, FakeClient(), bus, SessionStore(paths.sessions_dir), TaskStore(paths.tasks_dir), request_binding_store=bindings)
    event = await bridge.publish_raw_event({'type':'message.part.delta','sessionID':'oc-2','properties':{'sessionID':'oc-2','messageID':'m-2','partID':'p1','delta':'x'}})
    assert event['data']['request_id'] == 'req-2'
    assert event['data']['portal_request_id'] == 'req-2'
    assert event['data']['opencode_session_id'] == 'oc-2'
