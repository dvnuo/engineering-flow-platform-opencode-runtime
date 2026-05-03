import json
import pytest
from efp_opencode_adapter.chatlog_store import ChatLogStore
from efp_opencode_adapter.recovery import RecoveryManager
from efp_opencode_adapter.session_store import SessionRecord, SessionStore
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.state import ensure_state_dirs
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
