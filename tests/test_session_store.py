import json
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



def test_session_store_quarantines_corrupted_index_and_starts_empty(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    index = sessions_dir / "index.json"
    index.write_text("{ bad json", encoding="utf-8")

    store = SessionStore(sessions_dir)

    assert store.list_active() == []
    assert not index.exists()

    backups = list(sessions_dir.glob("index.json.corrupt-*"))
    assert backups
    assert backups[0].read_text(encoding="utf-8") == "{ bad json"


def test_session_store_bad_message_count_defaults_to_zero(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "index.json").write_text(
        json.dumps(
            {
                "sessions": {
                    "s1": {
                        "portal_session_id": "s1",
                        "opencode_session_id": "o1",
                        "title": "Chat",
                        "message_count": "bad",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    store = SessionStore(sessions_dir)
    rec = store.get("s1")

    assert rec is not None
    assert rec.message_count == 0
    assert rec.opencode_session_id == "o1"


def test_deleted_tombstone_not_revived_by_update(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    store.upsert(_rec("p1", "ses-1"))
    store.mark_deleted("p1")
    rec = store.update_after_chat("p1", "u", "a", None, None)
    assert rec.deleted is True
    assert store.get("p1").deleted is True
    assert store.list_active() == []


def test_replace_mutation_rejects_deleted(tmp_path):
    from efp_opencode_adapter.session_store import SessionDeletedError
    store = SessionStore(tmp_path / "sessions")
    store.upsert(_rec("p1", "ses-1"))
    store.mark_deleted("p1")
    import pytest
    with pytest.raises(SessionDeletedError):
        store.replace_opencode_session_after_mutation("p1", "ses-2", message_count=0)
