import json

from efp_opencode_adapter.chatlog_store import ChatLogStore


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _on_disk(chatlogs_dir, session_id="sid"):
    return json.loads((chatlogs_dir / f"{session_id}.json").read_text(encoding="utf-8"))


def _disk_event_types(chatlogs_dir, session_id="sid"):
    return [e["type"] for e in _on_disk(chatlogs_dir, session_id)["entries"][-1]["runtime_events"]]


def _memory_event_types(store, session_id="sid"):
    return [e["type"] for e in store.get(session_id)["entries"][-1]["runtime_events"]]


def test_chatlog_store_start_finish_reload_and_latest(tmp_path):
    s = ChatLogStore(tmp_path)

    s.start_entry(
        "../bad",
        request_id="r1",
        message="hello",
        runtime_events=[{"type": "execution.started"}],
        context_state={"current_state": "running"},
        llm_debug={"engine": "opencode"},
    )
    assert (tmp_path / "bad.json").exists()

    s.finish_entry(
        "../bad",
        request_id="r1",
        status="success",
        response="ok",
        runtime_events=[
            {"type": "execution.started"},
            {"type": "llm_thinking"},
            {"type": "complete"},
            {"type": "execution.completed"},
        ],
        events=[{"type": "complete"}],
        context_state={"current_state": "completed", "summary": "ok"},
        llm_debug={"engine": "opencode", "usage": {"requests": 1}},
    )

    d = s.get("../bad")
    entry = d["entries"][-1]
    assert entry["status"] == "success"
    assert entry["response"] == "ok"
    assert entry["context_state"]["current_state"] == "completed"
    assert entry["llm_debug"]["usage"]["requests"] == 1
    assert entry["finished_at"]

    types = {e["type"] for e in entry["runtime_events"]}
    assert "execution.started" in types
    assert "llm_thinking" in types
    assert "complete" in types
    assert "execution.completed" in types

    s2 = ChatLogStore(tmp_path)
    assert s2.get("../bad")["entries"][-1]["status"] == "success"

    s.start_entry("x", request_id="r2", message="m")
    assert s.latest_entry("x")["request_id"] == "r2"


def test_chatlog_append_event_persists_existing_entry(tmp_path):
    s = ChatLogStore(tmp_path)
    s.start_entry("sid", request_id="r1", message="hello")
    s.append_event("sid", request_id="r1", event={"type": "llm_thinking"})
    d = s.get("sid")
    assert d["entries"][-1]["runtime_events"][-1]["type"] == "llm_thinking"


def test_streamed_events_do_not_rewrite_the_file_once_per_event(tmp_path):
    """The bridge appends hundreds of events per turn; each one must not cost a
    full re-read and rewrite of the session file."""
    clock = _FakeClock()
    s = ChatLogStore(tmp_path, event_flush_interval_seconds=10.0, clock=clock)
    s.start_entry("sid", request_id="r1", message="hello")

    for i in range(50):
        clock.advance(0.05)
        s.append_event("sid", request_id="r1", event={"type": f"e{i}"})

    # The in-memory view every reader goes through is exact and immediate ...
    assert _memory_event_types(s) == [f"e{i}" for i in range(50)]
    # ... while the file was left alone: 50 appends inside one flush interval
    # produced zero extra writes.
    assert _disk_event_types(tmp_path) == []


def test_event_append_reaches_disk_once_the_flush_interval_elapses(tmp_path):
    clock = _FakeClock()
    s = ChatLogStore(tmp_path, event_flush_interval_seconds=2.0, clock=clock)
    s.start_entry("sid", request_id="r1", message="hello")

    s.append_event("sid", request_id="r1", event={"type": "early"})
    assert _disk_event_types(tmp_path) == []

    clock.advance(2.5)
    s.append_event("sid", request_id="r1", event={"type": "late"})
    assert _disk_event_types(tmp_path) == ["early", "late"]


def test_finish_entry_persists_the_coalesced_events_it_carries_over(tmp_path):
    clock = _FakeClock()
    s = ChatLogStore(tmp_path, event_flush_interval_seconds=10.0, clock=clock)
    s.start_entry("sid", request_id="r1", message="hello")
    s.append_event("sid", request_id="r1", event={"type": "tool.started"})
    s.append_event("sid", request_id="r1", event={"type": "tool.completed"})

    s.finish_entry("sid", request_id="r1", status="success", response="ok")

    entry = _on_disk(tmp_path)["entries"][-1]
    assert entry["status"] == "success"
    assert [e["type"] for e in entry["runtime_events"]] == ["tool.started", "tool.completed"]


def test_flush_all_persists_pending_event_appends(tmp_path):
    clock = _FakeClock()
    s = ChatLogStore(tmp_path, event_flush_interval_seconds=10.0, clock=clock)
    s.start_entry("sid", request_id="r1", message="hello")
    s.append_event("sid", request_id="r1", event={"type": "e0"})
    assert _disk_event_types(tmp_path) == []

    assert s.flush_all() == 1
    assert _disk_event_types(tmp_path) == ["e0"]
    # nothing left pending, so a second flush is a no-op
    assert s.flush_all() == 0


def test_deleted_session_is_not_served_from_memory(tmp_path):
    clock = _FakeClock()
    s = ChatLogStore(tmp_path, event_flush_interval_seconds=10.0, clock=clock)
    s.start_entry("sid", request_id="r1", message="hello")
    s.append_event("sid", request_id="r1", event={"type": "e0"})

    assert s.delete("sid") is True
    assert s.get("sid") is None
    assert s.latest_entry("sid") is None
    assert not (tmp_path / "sid.json").exists()


def test_chatlog_store_quarantines_corrupted_file_before_starting_new_entry(tmp_path):
    path = tmp_path / "sid.json"
    path.write_text("{ bad json", encoding="utf-8")

    s = ChatLogStore(tmp_path)
    s.start_entry(
        "sid",
        request_id="r1",
        message="hello",
        runtime_events=[{"type": "execution.started"}],
    )
    s.finish_entry(
        "sid",
        request_id="r1",
        status="success",
        response="ok",
        runtime_events=[
            {"type": "execution.started"},
            {"type": "llm_thinking"},
            {"type": "complete"},
            {"type": "execution.completed"},
        ],
    )

    current = s.get("sid")
    assert current is not None
    assert current["session_id"] == "sid"
    assert current["entries"][-1]["status"] == "success"
    assert current["entries"][-1]["response"] == "ok"

    backups = list(tmp_path.glob("sid.json.corrupt-*"))
    assert backups
    assert backups[0].read_text(encoding="utf-8") == "{ bad json"
