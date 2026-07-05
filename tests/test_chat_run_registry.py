import asyncio

from efp_opencode_adapter.chat_run_registry import (
    RETAINED_EVENT_TAIL_ITEMS,
    ChatRunRegistry,
)


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


def test_chat_run_registry_compacts_retained_event_streams():
    registry = ChatRunRegistry()
    registry.start(session_id="s1", request_id="r1")
    events = [{"type": "assistant.delta", "data": {"delta": str(i)}} for i in range(500)]

    registry.complete(
        "r1",
        {
            "ok": True,
            "completion_state": "completed",
            "response": "done",
            "events": events,
            "runtime_events": events,
        },
    )

    final = registry.get("r1").to_payload()["final_payload"]
    assert final["response"] == "done"
    assert len(final["runtime_events"]) == RETAINED_EVENT_TAIL_ITEMS
    assert final["runtime_events"][-1] == events[-1]
    assert final["runtime_events_count"] == 500
    assert final["runtime_events_truncated"] is True
    assert len(final["events"]) == RETAINED_EVENT_TAIL_ITEMS


def test_chat_run_registry_keeps_small_event_lists_intact():
    registry = ChatRunRegistry()
    registry.start(session_id="s1", request_id="r1")
    events = [{"type": "assistant.delta", "data": {"delta": "x"}}]

    registry.complete(
        "r1",
        {"ok": True, "completion_state": "completed", "runtime_events": events},
    )

    final = registry.get("r1").to_payload()["final_payload"]
    assert final["runtime_events"] == events
    assert "runtime_events_count" not in final
    assert "runtime_events_truncated" not in final


def test_chat_run_registry_releases_task_reference_on_terminal():
    async def scenario():
        registry = ChatRunRegistry()
        record = registry.start(session_id="s1", request_id="r1")

        async def _noop():
            return {"ok": True}

        task = asyncio.create_task(_noop())
        registry.attach_task("r1", task)
        await task
        assert record.task is task

        registry.complete("r1", {"ok": True, "completion_state": "completed"})
        assert record.task is None

    asyncio.run(scenario())


def test_chat_run_registry_marks_stale_running_records_failed_and_prunable():
    registry = ChatRunRegistry(max_records=2, stale_running_seconds=3600)
    stuck = registry.start(session_id="s1", request_id="stuck")
    stuck.updated_at = "2000-01-01T00:00:00Z"

    registry.start(session_id="s1", request_id="new1")

    stuck_record = registry.get("stuck")
    assert stuck_record.state == "failed"
    assert stuck_record.terminal is True
    assert stuck_record.error_payload["error"] == "chat_run_stale"

    registry.start(session_id="s1", request_id="new2")
    assert registry.get("stuck") is None
    assert registry.get("new1") is not None
    assert registry.get("new2") is not None
