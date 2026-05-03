from pathlib import Path

from efp_opencode_adapter.session_store import SessionRecord, SessionStore


def _rec(pid: str = "p1", sid: str = "ses-1") -> SessionRecord:
    return SessionRecord(pid, sid, "t", None, None, "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00", "", 0)


def test_store_init_and_persist(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    assert store.list_active() == []
    store.upsert(_rec())
    assert store.index_path.exists()

    store2 = SessionStore(tmp_path / "sessions")
    got = store2.get("p1")
    assert got is not None
    assert got.opencode_session_id == "ses-1"


def test_rename_delete_clear_update(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    store.upsert(_rec("p1", "ses-1"))
    store.upsert(_rec("p2", "ses-2"))
    r = store.rename("p1", "new")
    assert r.title == "new"
    d = store.mark_deleted("p1")
    assert d and d.deleted is True
    cleared = store.clear()
    assert len(cleared) == 1
    store.upsert(_rec("p3", "ses-3"))
    before = store.get("p3").updated_at
    up = store.update_after_chat("p3", "hello", "echo", "m1", "a1")
    assert up.message_count >= 2
    assert up.last_message == "echo"
    assert up.updated_at != before
