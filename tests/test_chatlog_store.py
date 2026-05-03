from efp_opencode_adapter.chatlog_store import ChatLogStore


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
