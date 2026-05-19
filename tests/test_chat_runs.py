import json

from efp_opencode_adapter.chat_run_store import ChatRunStore


def test_chat_run_store_start_get_active_attach_detach_and_persistence(tmp_path):
    path = tmp_path / "chat_runs.json"
    store = ChatRunStore(path)

    run = store.start_run(
        request_id="req-1",
        portal_session_id="sess-1",
        opencode_session_id="ses-1",
        user_message_id="u-1",
        status="running",
        metadata={"token": "ghp_SECRET"},
    )

    assert run.request_id == "req-1"
    assert store.get("req-1").status == "running"
    assert store.active_for_session("sess-1").request_id == "req-1"

    store.attach_stream("req-1")
    assert store.get("req-1").stream_state == "attached"
    assert store.get("req-1").status == "stream_attached"

    store.detach_stream("req-1", reason="client_disconnected")
    assert store.get("req-1").stream_state == "detached"
    assert store.get("req-1").status == "stream_detached"

    store.complete_run("req-1", {"completion_state": "completed", "response": "done", "assistant_message_id": "a-1", "assistant_message_ids": ["a-1"]})
    assert store.get("req-1").status == "completed"
    assert store.get("req-1").stream_state == "closed"
    assert store.active_for_session("sess-1") is None

    reloaded = ChatRunStore(path)
    loaded = reloaded.get("req-1")
    assert loaded.status == "completed"
    assert loaded.last_response_text == "done"
    assert loaded.assistant_message_ids == ["a-1"]

    serialized = path.read_text(encoding="utf-8")
    assert "ghp_SECRET" not in serialized
    assert "***REDACTED***" in serialized


def test_chat_run_store_complete_incomplete_failed_and_list(tmp_path):
    store = ChatRunStore(tmp_path / "chat_runs.json")
    store.start_run(request_id="req-c", portal_session_id="sess", opencode_session_id="ses", status="running")
    store.complete_run("req-c", {"completion_state": "completed", "response": "ok"})

    store.start_run(request_id="req-i", portal_session_id="sess", opencode_session_id="ses", status="running")
    store.mark_incomplete("req-i", "timeout", {"completion_state": "incomplete", "incomplete_reason": "timeout", "response": "partial"})

    store.start_run(request_id="req-f", portal_session_id="sess", opencode_session_id="ses", status="running")
    store.mark_failed("req-f", "boom", {"completion_state": "error", "detail": "boom"})

    assert store.get("req-c").status == "completed"
    assert store.get("req-i").status == "incomplete"
    assert store.get("req-i").incomplete_reason == "timeout"
    assert store.get("req-f").status == "failed"
    assert [run.request_id for run in store.list_for_session("sess", limit=3)] == ["req-f", "req-i", "req-c"]


def test_chat_run_store_update_runtime_event_projection_is_sanitized(tmp_path):
    store = ChatRunStore(tmp_path / "chat_runs.json")
    store.start_run(request_id="req-1", portal_session_id="sess-1", opencode_session_id="ses-1")

    store.update_from_runtime_event(
        "req-1",
        {
            "type": "message.delta",
            "request_id": "req-1",
            "session_id": "sess-1",
            "data": {"delta": "hello ", "message_role": "assistant", "part_type": "text", "token": "secret"},
            "created_at": "2026-05-19T00:00:00+00:00",
        },
    )
    store.update_assistant_projection("req-1", text="world", assistant_message_id="a-1", display_blocks=[{"text": "world", "api_key": "secret"}])

    public = store.to_public_dict(store.get("req-1"))
    assert public["last_response_text"] == "world"
    assert public["assistant_message_id"] == "a-1"
    assert "secret" not in json.dumps(public)
