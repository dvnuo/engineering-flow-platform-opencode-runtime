import pytest

from efp_opencode_adapter.opencode_ids import is_opencode_message_id, new_opencode_message_id, require_opencode_message_id


def test_new_opencode_message_id_shape_and_uniqueness():
    first = new_opencode_message_id()
    second = new_opencode_message_id()
    assert first.startswith("msg_")
    assert second.startswith("msg_")
    assert first != second


def test_opencode_message_id_validation():
    assert is_opencode_message_id("msg_abc") is True
    assert is_opencode_message_id("portal-user-123") is False
    with pytest.raises(ValueError):
        require_opencode_message_id("portal-user-123")
