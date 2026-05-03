import pytest

from efp_opencode_adapter.task_store import TaskRecord, TaskStore, utc_now_iso


def _record(task_id='t1'):
    return TaskRecord(task_id=task_id, task_type='generic_agent_task', request_id='r1', status='accepted', portal_session_id='p1', opencode_session_id='o1', input_payload={}, metadata={}, output_payload={}, artifacts={}, runtime_events=[], error=None, created_at=utc_now_iso())


def test_save_get_reload_append_update(tmp_path):
    store = TaskStore(tmp_path)
    store.save(_record())
    assert (tmp_path / 't1.json').exists()
    assert store.get('t1').task_type == 'generic_agent_task'
    store2 = TaskStore(tmp_path)
    assert store2.get('t1').request_id == 'r1'
    store2.append_event('t1', {'type': 'task.accepted'})
    assert len(store2.get('t1').runtime_events) == 1
    store2.update('t1', status='success', output_payload={'summary': 'ok'})
    got = store2.get('t1')
    assert got.status == 'success'
    assert got.output_payload['summary'] == 'ok'


def test_invalid_task_id_rejected(tmp_path):
    store = TaskStore(tmp_path)
    with pytest.raises(ValueError):
        store.save(_record('../x'))
