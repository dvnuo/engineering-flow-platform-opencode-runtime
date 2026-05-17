import json

from efp_opencode_adapter.user_display_store import UserDisplayStore, sanitize_display_attachments


def test_put_get_user_message_roundtrip(tmp_path):
    store = UserDisplayStore(tmp_path / "user_display_messages.json")

    record = store.put_user_message(
        portal_session_id="portal_1",
        opencode_session_id="ses_1",
        opencode_message_id="msg_1",
        display_content="/jira-bulk-create-from-csv example: https://jira.company.com/browse/MMGFX-13887",
        display_attachments=[{"file_id": "file_1", "name": "cases.csv", "content_type": "text/csv", "size": 123, "type": "file", "parsed": True}],
        metadata={"source": "portal_original_user_message"},
    )

    assert record["role"] == "user"
    assert record["display_content"] == "/jira-bulk-create-from-csv example: https://jira.company.com/browse/MMGFX-13887"
    assert record["display_attachments"] == [{"file_id": "file_1", "name": "cases.csv", "content_type": "text/csv", "size": 123, "type": "file", "parsed": True}]

    reloaded = UserDisplayStore(tmp_path / "user_display_messages.json")
    assert reloaded.get_user_message("ses_1", "msg_1", "portal_1")["display_content"] == record["display_content"]
    persisted = json.loads((tmp_path / "user_display_messages.json").read_text(encoding="utf-8"))
    assert persisted["version"] == 1
    assert "ses_1:msg_1" in persisted["messages"]


def test_sanitize_display_attachments_drops_text_url_and_raw_content():
    sanitized = sanitize_display_attachments(
        [
            "file_string",
            {
                "file_id": " file_1 ",
                "id": " id_1 ",
                "name": " cases.csv ",
                "filename": " cases.csv ",
                "content_type": " text/csv ",
                "mime": " text/csv ",
                "size": 123,
                "type": "csv",
                "parsed": True,
                "parse_error": " none ",
                "url": "https://files.invalid/cases.csv",
                "previewUrl": "https://files.invalid/preview",
                "text": "summary,steps\nA,B",
                "content": "summary,steps\nA,B",
                "data": "summary,steps\nA,B",
                "raw": "summary,steps\nA,B",
                "base64": "c3VtbWFyeSxzdGVwcw==",
            },
            {"name": "", "size": "123", "type": "image"},
        ]
    )

    assert sanitized[0] == {"file_id": "file_string", "id": "file_string", "type": "file"}
    assert sanitized[1] == {
        "file_id": "file_1",
        "id": "id_1",
        "name": "cases.csv",
        "filename": "cases.csv",
        "content_type": "text/csv",
        "mime": "text/csv",
        "size": 123,
        "type": "file",
        "parsed": True,
        "parse_error": "none",
    }
    assert sanitized[2] == {"type": "image"}

    serialized = json.dumps(sanitized)
    for forbidden in ["url", "previewUrl", "summary,steps", "base64", "raw"]:
        assert forbidden not in serialized


def test_get_user_message_fallback_by_message_id_and_portal_session(tmp_path):
    store = UserDisplayStore(tmp_path / "user_display_messages.json")
    store.put_user_message(
        portal_session_id="portal_1",
        opencode_session_id="old_ses",
        opencode_message_id="msg_1",
        display_content="original",
        display_attachments=[],
        metadata={"source": "portal_original_user_message"},
    )

    assert store.get_user_message("new_ses", "msg_1", "portal_1")["display_content"] == "original"
    assert store.get_user_message("new_ses", "msg_1", "other_portal") is None
