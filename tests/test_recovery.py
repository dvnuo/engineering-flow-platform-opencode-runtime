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
    task = TaskRecord(task_id='running-task', task_type='generic_agent_task', request_id='req-1', status='running', portal_session_id='portal-1', opencode_session_id='oc-1', input_payload={}, metadata={}, output_payload={}, artifacts={}, runtime_events=[], error=None, created_at=utc_now_iso())
    TaskStore(paths.tasks_dir).save(task)
    rm = RecoveryManager(settings=st,state_paths=paths,session_store=ss,chatlog_store=cs,opencode_client=FakeOpenCodeClient())
    sm = await rm.recover()
    assert sm['corrupted_chatlogs'] >= 1
    assert ss.get('p1').partial_recovery is True
    assert TaskStore(paths.tasks_dir).get('running-task').status == 'blocked'


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
async def test_recovery_marks_all_active_tasks_without_list_cap(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    monkeypatch.setenv('EFP_OPENCODE_TASKS_LIST_MAX_RECORDS', '1')
    monkeypatch.setenv('EFP_OPENCODE_TASKS_SCAN_MAX_RECORDS', '1')
    st = Settings.from_env(); paths = ensure_state_dirs(st)
    store = TaskStore(paths.tasks_dir)
    for idx in range(3):
        store.save(TaskRecord(task_id=f't{idx}', task_type='generic_agent_task', request_id=f'req-{idx}', status='running', portal_session_id=f'portal-{idx}', opencode_session_id=f'oc-{idx}', input_payload={}, metadata={}, output_payload={}, artifacts={}, runtime_events=[], error=None, created_at=utc_now_iso()))

    rm = RecoveryManager(settings=st,state_paths=paths,session_store=SessionStore(paths.sessions_dir),chatlog_store=ChatLogStore(paths.chatlogs_dir),opencode_client=FakeOpenCodeClient())
    summary = await rm.recover()

    assert summary['tasks_marked_blocked'] == 3
    assert [store.get(f't{idx}').status for idx in range(3)] == ['blocked', 'blocked', 'blocked']


@pytest.mark.asyncio
async def test_recovery_marks_oversized_active_task_blocked_without_loading_full_record(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    st = Settings.from_env(); paths = ensure_state_dirs(st)
    store = TaskStore(paths.tasks_dir)
    record = TaskRecord(task_id='oversized-active', task_type='generic_agent_task', request_id='req-big', status='running', portal_session_id='portal-big', opencode_session_id='oc-big', input_payload={}, metadata={}, output_payload={'raw': 'x' * 5000}, artifacts={}, runtime_events=[], error=None, created_at=utc_now_iso())
    store.save(record)
    path = paths.tasks_dir / 'oversized-active.json'
    original_size = path.stat().st_size
    monkeypatch.setenv('EFP_OPENCODE_TASKS_LOAD_MAX_FILE_BYTES', '100')

    rm = RecoveryManager(settings=st,state_paths=paths,session_store=SessionStore(paths.sessions_dir),chatlog_store=ChatLogStore(paths.chatlogs_dir),opencode_client=FakeOpenCodeClient())
    summary = await rm.recover()

    assert summary['tasks_marked_blocked'] == 1
    assert path.stat().st_size < original_size
    monkeypatch.setenv('EFP_OPENCODE_TASKS_LOAD_MAX_FILE_BYTES', '5000')
    got = TaskStore(paths.tasks_dir).get('oversized-active')
    assert got.status == 'blocked'
    assert got.output_payload['error_code'] == 'adapter_restarted_task_recovery_required'


@pytest.mark.asyncio
async def test_recovery_does_not_count_task_when_blocked_record_is_not_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    st = Settings.from_env(); paths = ensure_state_dirs(st)
    store = TaskStore(paths.tasks_dir)
    record = TaskRecord(task_id='cannot-persist-blocked', task_type='generic_agent_task', request_id='req-cannot-persist', status='running', portal_session_id='portal-cannot-persist', opencode_session_id='oc-cannot-persist', input_payload={}, metadata={}, output_payload={}, artifacts={}, runtime_events=[], error=None, created_at=utc_now_iso())
    store.save(record)
    monkeypatch.setenv('EFP_OPENCODE_TASKS_PERSIST_MAX_FILE_BYTES', '1')

    rm = RecoveryManager(settings=st,state_paths=paths,session_store=SessionStore(paths.sessions_dir),chatlog_store=ChatLogStore(paths.chatlogs_dir),opencode_client=FakeOpenCodeClient())
    summary = await rm.recover()

    assert summary['tasks_marked_blocked'] == 0
    assert TaskStore(paths.tasks_dir).get('cannot-persist-blocked').status == 'running'
