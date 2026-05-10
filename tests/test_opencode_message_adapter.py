from efp_opencode_adapter.opencode_message_adapter import (
    find_latest_assistant_completion,
    extract_reasoning_texts_from_parts,
    extract_visible_text_from_parts,
    extract_last_assistant_visible_text,
    message_to_visible_text,
)


def test_visible_text_extraction_filters_non_text_parts():
    parts = [
        {"type": "step-start", "id": "s1"},
        {"type": "reasoning", "text": "hidden reasoning"},
        {"type": "text", "text": "Hi. How can I help?"},
        {"type": "step-finish", "reason": "stop"},
    ]
    assert extract_visible_text_from_parts(parts) == "Hi. How can I help?"
    assert extract_reasoning_texts_from_parts(parts) == ["hidden reasoning"]
    assert "reasoning" not in message_to_visible_text({"role": "assistant", "parts": parts})


def test_extract_last_assistant_visible_text_never_falls_back_user():
    payload = {"messages": [{"role": "user", "parts": [{"type": "text", "text": "HI"}]}]}
    assert extract_last_assistant_visible_text(payload) == ""
    payload2 = {"messages": [{"role": "user", "parts": [{"type": "text", "text": "HI"}]}, {"role": "assistant", "parts": [{"type": "text", "text": "Hello"}]}]}
    assert extract_last_assistant_visible_text(payload2) == "Hello"


def test_message_to_visible_text_typed_parts_do_not_fallback_to_text_field():
    msg = {"role": "assistant", "parts": [{"type": "reasoning", "text": "hidden reasoning"}], "text": "hidden reasoning"}
    assert message_to_visible_text(msg) == ""


def test_message_to_visible_text_prefers_visible_text_part_over_legacy_text_field():
    msg = {
        "role": "assistant",
        "parts": [{"type": "reasoning", "text": "hidden"}, {"type": "text", "text": "visible"}],
        "text": "hidden",
    }
    assert message_to_visible_text(msg) == "visible"


def test_progress_text_is_not_terminal():
    payload = {"messages": [{"id": "a1", "role": "assistant", "parts": [{"type": "text", "text": "I am fetching the Confluence page now and will summarize the agenda once I have the content"}, {"type": "tool", "status": "running"}]}]}
    out = find_latest_assistant_completion(payload)
    assert out["completion_state"] != "completed"


def test_final_text_after_tool_is_terminal():
    payload = {"messages": [{"id": "a1", "role": "assistant", "parts": [{"type": "text", "text": "I am fetching the Confluence page now and will summarize the agenda once I have the content"}, {"type": "tool", "status": "running"}]}, {"id": "a2", "role": "assistant", "parts": [{"type": "text", "text": "Agenda: kickoff, review, action items"}, {"type": "step-finish", "reason": "stop"}], "finish_reason": "stop"}]}
    out = find_latest_assistant_completion(payload)
    assert out["completion_state"] == "completed"
    assert out["text"].startswith("Agenda:")


def test_pending_tool_blocks_terminal():
    payload = {"messages": [{"id": "a1", "role": "assistant", "parts": [{"type": "text", "text": "Working..."}, {"type": "tool", "status": "running"}]}]}
    out = find_latest_assistant_completion(payload)
    assert out["completion_state"] in {"pending", "incomplete"}


def test_tool_error_returns_error_state():
    payload = {"messages": [{"id": "a1", "role": "assistant", "parts": [{"type": "tool", "status": "error", "error": "permission denied"}]}]}
    out = find_latest_assistant_completion(payload)
    assert out["completion_state"] == "error"
    assert "permission" in str(out["diagnostics"].get("error_summary", "")).lower()


def test_progress_text_with_stop_is_not_terminal():
    payload = {"messages": [{"id": "a1", "role": "assistant", "finish_reason": "stop", "parts": [{"type": "text", "text": "I am fetching the Confluence page now and will summarize the agenda once I have the content"}]}]}
    out = find_latest_assistant_completion(payload)
    assert out["completion_state"] != "completed"


def test_pending_permission_returns_blocked():
    payload = {"messages": [{"id":"a1","role":"assistant","parts":[{"type":"permission","status":"pending"}]}]}
    out = find_latest_assistant_completion(payload)
    assert out["completion_state"] == "blocked"


def test_empty_assistant_is_incomplete():
    payload = {"messages": [{"id":"a1","role":"assistant","parts":[]}]}
    out = find_latest_assistant_completion(payload)
    assert out["completion_state"] == "incomplete"


def test_finish_reason_tool_use_is_not_completed():
    payload = {"messages": [{"id":"a1","role":"assistant","finish_reason":"tool_use","parts":[{"type":"text","text":"final?"}]}]}
    out = find_latest_assistant_completion(payload)
    assert out["completion_state"] != "completed"
