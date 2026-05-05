import json
import pytest
from efp_opencode_adapter.chatlog_store import ChatLogStore
from efp_opencode_adapter.recovery import RecoveryManager
from efp_opencode_adapter.session_store import SessionRecord, SessionStore
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.state import ensure_state_dirs
from efp_opencode_adapter.task_store import TaskRecord, TaskStore, utc_now_iso
from test_t06_helpers import FakeOpenCodeClient

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
