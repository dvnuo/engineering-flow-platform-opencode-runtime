from efp_opencode_adapter.chat_run_registry import ChatRunRegistry


def test_chat_run_registry_tracks_final_payload():
    registry = ChatRunRegistry()
    registry.start(session_id="s1", request_id="r1")

    registry.record_event("r1", {"created_at": "2026-06-17T00:00:00Z"})
    registry.complete("r1", {"ok": True, "completion_state": "completed", "response": "done"})

    payload = registry.get("r1").to_payload()
    assert payload["engine"] == "opencode"
    assert payload["state"] == "completed"
    assert payload["terminal"] is True
    assert payload["latest_event_seq"] == 1
    assert payload["final_payload"]["response"] == "done"


def test_chat_run_registry_cancel_is_not_overwritten_by_late_failure():
    registry = ChatRunRegistry()
    registry.start(session_id="s1", request_id="r1")

    assert registry.cancel("r1") is True
    registry.fail("r1", {"error": "late"})

    payload = registry.get("r1").to_payload()
    assert payload["state"] == "cancelled"
    assert payload["terminal"] is True


def test_chat_run_registry_prunes_only_terminal_records():
    registry = ChatRunRegistry(max_records=2)
    registry.start(session_id="s1", request_id="old")
    registry.complete("old", {"ok": True, "completion_state": "completed"})
    registry.start(session_id="s1", request_id="running")
    registry.start(session_id="s1", request_id="new")

    assert registry.get("old") is None
    assert registry.get("running") is not None
    assert registry.get("new") is not None
