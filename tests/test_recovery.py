import json
import pytest
from efp_opencode_adapter.app_keys import CHAT_RUN_STORE_KEY, EVENT_BUS_KEY
from efp_opencode_adapter.chatlog_store import ChatLogStore
from efp_opencode_adapter.opencode_client import OpenCodeClientError
from efp_opencode_adapter.recovery import RecoveryManager
from efp_opencode_adapter.server import create_app, reconcile_chat_runs_on_startup
from efp_opencode_adapter.session_store import SessionRecord, SessionStore
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.state import ensure_state_dirs
from efp_opencode_adapter.task_store import TaskRecord, TaskStore, utc_now_iso
from test_t06_helpers import FakeOpenCodeClient


class _StartupRunStateClient(FakeOpenCodeClient):
    def __init__(self, *, state: str = "idle", missing: bool = False):
        super().__init__()
        self.state = state
        self.missing = missing

    async def get_session_status(self):
        return {"sessions": {sid: {"state": self.state} for sid in self.sessions}}

    async def list_messages(self, session_id):
        if self.missing:
            raise OpenCodeClientError("not found", status=404)
        return await super().list_messages(session_id)


@pytest.mark.asyncio
async def test_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    st = Settings.from_env(); paths = ensure_state_dirs(st)
    ss = SessionStore(paths.sessions_dir)
    rec = SessionRecord('p1','missing','t',None,None,'a','b','',0)
    ss.upsert(rec)
    cs = ChatLogStore(paths.chatlogs_dir)
    cs.start_entry('p1', request_id='r1', message='m')
    (paths.chatlogs_dir/'bad.json').write_text('{bad', encoding='utf-8')
    task = paths.tasks_dir/'running-task.json'
    task.write_text(json.dumps({'status':'running','runtime_events':[]}), encoding='utf-8')
    rm = RecoveryManager(settings=st,state_paths=paths,session_store=ss,chatlog_store=cs,opencode_client=FakeOpenCodeClient())
    sm = await rm.recover()
    assert sm['corrupted_chatlogs'] >= 1
    assert ss.get('p1').partial_recovery is True
    assert json.loads(task.read_text())['status'] == 'blocked'


@pytest.mark.asyncio
async def test_recovery_marks_running_task_blocked_without_extra_top_level_error_code(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    st = Settings.from_env(); paths = ensure_state_dirs(st)
    rec = TaskRecord(task_id='t1', task_type='generic_agent_task', request_id='req-1', status='running', portal_session_id='portal-1', opencode_session_id='oc-1', input_payload={}, metadata={}, output_payload={}, artifacts={}, runtime_events=[], error=None, created_at=utc_now_iso())
    TaskStore(paths.tasks_dir).save(rec)
    rm = RecoveryManager(settings=st,state_paths=paths,session_store=SessionStore(paths.sessions_dir),chatlog_store=ChatLogStore(paths.chatlogs_dir),opencode_client=FakeOpenCodeClient())
    await rm.recover()
    got = TaskStore(paths.tasks_dir).get('t1')
    assert got and got.status == 'blocked'
    assert got.output_payload['error_code'] == 'adapter_restarted_task_recovery_required'
    raw = json.loads((paths.tasks_dir/'t1.json').read_text())
    assert 'error_code' not in raw
    assert got.runtime_events[-1]['session_id'] == 'portal-1'


@pytest.mark.asyncio
async def test_startup_reconcile_marks_stale_detached_chat_run(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    fake = _StartupRunStateClient(missing=True)
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[CHAT_RUN_STORE_KEY].start_run(request_id='req-stale', portal_session_id='portal-1', opencode_session_id='missing', status='stream_detached', stream_state='detached')

    summary = await reconcile_chat_runs_on_startup(app)

    assert summary['chat_runs_validated'] == 1
    assert summary['chat_runs_stale_marked'] == 1
    assert summary['chat_runs_still_active'] == 0
    assert app[CHAT_RUN_STORE_KEY].get('req-stale').status == 'stale'
    assert app[CHAT_RUN_STORE_KEY].active_for_session('portal-1') is None
    assert app[EVENT_BUS_KEY].recent_events(request_id='req-stale')[-1]['type'] == 'opencode.session.missing'


@pytest.mark.asyncio
async def test_startup_reconcile_keeps_opencode_active_chat_run(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    fake = _StartupRunStateClient(state='running')
    fake.sessions['ses-active'] = {'id': 'ses-active', 'title': 'Chat'}
    fake.messages['ses-active'] = []
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[CHAT_RUN_STORE_KEY].start_run(request_id='req-active', portal_session_id='portal-1', opencode_session_id='ses-active', status='running')

    summary = await reconcile_chat_runs_on_startup(app)

    assert summary['chat_runs_validated'] == 1
    assert summary['chat_runs_stale_marked'] == 0
    assert summary['chat_runs_still_active'] == 1
    assert app[CHAT_RUN_STORE_KEY].get('req-active').status == 'running'
    assert app[CHAT_RUN_STORE_KEY].get('req-active').metadata['opencode_active'] is True
