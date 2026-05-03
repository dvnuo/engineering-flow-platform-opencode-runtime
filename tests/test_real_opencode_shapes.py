from efp_opencode_adapter.chat_api import extract_assistant_text
from efp_opencode_adapter.sessions_api import _to_efp_messages


def test_to_efp_messages_supports_opencode_info_parts_shape():
    raw = [
        {"info": {"id": "u1", "role": "user", "time": {"created": 1710000000000}}, "parts": [{"type": "text", "text": "hello"}]},
        {"info": {"id": "a1", "role": "assistant", "time": {"created": 1710000001000}}, "parts": [{"type": "text", "text": "hi"}]},
    ]
    out = _to_efp_messages(raw)
    assert out[0]["id"] == "u1"
    assert out[0]["role"] == "user"
    assert out[0]["content"] == "hello"
    assert out[0]["timestamp"]
    assert out[1]["id"] == "a1"
    assert out[1]["role"] == "assistant"
    assert out[1]["content"] == "hi"


def test_extract_assistant_text_supports_opencode_info_parts_shape():
    payload = {"info": {"id": "a1", "role": "assistant"}, "parts": [{"type": "text", "text": "assistant text"}]}
    assert extract_assistant_text(payload) == "assistant text"


def test_extract_assistant_text_finds_last_assistant_in_opencode_list():
    payload = [
        {"info": {"id": "u1", "role": "user"}, "parts": [{"type": "text", "text": "user text"}]},
        {"info": {"id": "a1", "role": "assistant"}, "parts": [{"type": "text", "text": "assistant text"}]},
    ]
    assert extract_assistant_text(payload) == "assistant text"
