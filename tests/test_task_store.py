import pytest
import json

from efp_opencode_adapter.task_store import (
    TaskRecord,
    TaskRecordLoadLimitExceeded,
    TaskRecordPersistenceLimitExceeded,
    TaskRecordReadError,
    TaskStore,
    utc_now_iso,
)


def _record(task_id='t1'):
    return TaskRecord(task_id=task_id, task_type='generic_agent_task', request_id='r1', status='accepted', portal_session_id='p1', opencode_session_id='o1', input_payload={}, metadata={}, output_payload={}, artifacts={}, runtime_events=[], error=None, created_at=utc_now_iso())


def test_save_get_reload_append_update(tmp_path):
    store = TaskStore(tmp_path)
    store.save(_record())
    store.save(_record('t2'))
    store.update('t2', status='success', output_payload={'summary': 'ok'})
    assert (tmp_path / 't1.json').exists()
    assert store.get('t1').task_type == 'generic_agent_task'
    store2 = TaskStore(tmp_path)
    assert store2.get('t1').request_id == 'r1'
    assert [record.task_id for record in store2.list_active()] == ['t1']
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


def test_list_all_and_get_honor_load_limits(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    for task_id in ("a", "b", "c"):
        store.save(_record(task_id))

    monkeypatch.setenv("EFP_OPENCODE_TASKS_LIST_MAX_RECORDS", "2")
    monkeypatch.setenv("EFP_OPENCODE_TASKS_SCAN_MAX_RECORDS", "10")
    assert [record.task_id for record in store.list_all()] == ["a", "b"]

    large = _record("large")
    (tmp_path / "large.json").write_text(
        json.dumps({**large.__dict__, "output_payload": {"raw": "x" * 1000}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("EFP_OPENCODE_TASKS_LOAD_MAX_FILE_BYTES", "100")
    with pytest.raises(TaskRecordLoadLimitExceeded) as exc_info:
        store.get("large")
    assert exc_info.value.task_id == "large"


def test_oversized_files_count_toward_scan_limit(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    big = _record("big")
    valid = _record("valid")
    for name in ("a-big", "b-big"):
        (tmp_path / f"{name}.json").write_text(
            json.dumps({**big.__dict__, "task_id": name, "output_payload": {"raw": "x" * 1000}}),
            encoding="utf-8",
        )
    store.save(valid)

    monkeypatch.setenv("EFP_OPENCODE_TASKS_SCAN_MAX_RECORDS", "2")
    monkeypatch.setenv("EFP_OPENCODE_TASKS_LOAD_MAX_FILE_BYTES", "100")
    assert store.list_all() == []
    assert store.list_active() == []


def test_save_minimizes_oversized_record(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_OPENCODE_TASKS_PERSIST_MAX_FILE_BYTES", "1600")
    store = TaskStore(tmp_path)
    record = _record("huge")
    record.input_payload = {"prompt": "p" * 5000}
    record.output_payload = {"summary": "large output", "raw": "x" * 5000}
    record.runtime_events = [{"type": "step", "value": "y" * 200} for _ in range(20)]

    store.save(record)

    raw = json.loads((tmp_path / "huge.json").read_text(encoding="utf-8"))
    assert raw["input_payload"]["_omitted"] is True
    assert raw["output_payload"]["payload_omitted_from_persistence"] is True
    assert raw["output_payload"]["summary"] == "large output"
    assert len((tmp_path / "huge.json").read_bytes()) <= 1600


def test_save_uses_ultra_minimal_record_before_giving_up(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_OPENCODE_TASKS_PERSIST_MAX_FILE_BYTES", "520")
    store = TaskStore(tmp_path)
    record = _record("tiny")
    record.input_payload = {"prompt": "p" * 5000}
    record.metadata = {"task_id": "tiny", "extra": "m" * 5000}
    record.output_payload = {"summary": "s" * 1000, "raw": "x" * 5000}
    record.runtime_events = [{"type": "step", "value": "y" * 200} for _ in range(20)]
    record.pending_permission_ids = ["perm-" + ("z" * 200)]

    store.save(record)

    raw = json.loads((tmp_path / "tiny.json").read_text(encoding="utf-8"))
    assert raw["input_payload"] == {}
    assert raw["metadata"] == {}
    assert raw["runtime_events"] == []
    assert raw["output_payload"]["record_minimized_from_persistence"] is True
    assert store.get("tiny") is not None


def test_save_preserves_existing_file_when_encoding_is_impossible(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    record = _record("preserve")
    store.save(record)
    original = (tmp_path / "preserve.json").read_text(encoding="utf-8")

    monkeypatch.setenv("EFP_OPENCODE_TASKS_PERSIST_MAX_FILE_BYTES", "1")
    record.status = "success"
    record.output_payload = {"summary": "x" * 5000}
    returned = store.save(record)

    assert (tmp_path / "preserve.json").read_text(encoding="utf-8") == original
    assert returned.status == "accepted"
    assert store.get("preserve").status == "accepted"


def test_save_raises_when_new_record_cannot_be_encoded(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    monkeypatch.setenv("EFP_OPENCODE_TASKS_PERSIST_MAX_FILE_BYTES", "1")

    with pytest.raises(TaskRecordPersistenceLimitExceeded) as exc_info:
        store.save(_record("impossible"))

    assert exc_info.value.task_id == "impossible"
    assert not (tmp_path / "impossible.json").exists()


def test_get_raises_when_existing_record_is_unreadable(tmp_path):
    store = TaskStore(tmp_path)
    (tmp_path / "broken.json").write_text("{not-json", encoding="utf-8")

    with pytest.raises(TaskRecordReadError) as exc_info:
        store.get("broken")

    assert exc_info.value.task_id == "broken"
    assert exc_info.value.code == "task_record_unreadable"


def test_find_for_opencode_event_uses_message_or_single_active_match(tmp_path):
    store = TaskStore(tmp_path)
    first = _record("first")
    first.opencode_session_id = "oc"
    first.opencode_message_id = "msg-first"
    second = _record("second")
    second.opencode_session_id = "oc"
    second.opencode_message_id = "msg-second"
    second.status = "success"
    store.save(first)
    store.save(second)

    assert store.find_for_opencode_event("oc", {"msg-second"}).task_id == "second"
    assert store.find_for_opencode_event("oc", set()).task_id == "first"
