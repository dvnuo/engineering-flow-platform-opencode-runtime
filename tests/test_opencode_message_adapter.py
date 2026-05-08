from efp_opencode_adapter.opencode_message_adapter import (
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
