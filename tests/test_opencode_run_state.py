import pytest

from efp_opencode_adapter.opencode_client import OpenCodeClientError
from efp_opencode_adapter.opencode_run_state import (
    is_opencode_status_active,
    is_opencode_status_terminal_or_idle,
    resolve_opencode_run_state,
)


class _RunStateClient:
    def __init__(self, *, status=None, messages=None, children=None, missing_messages: bool = False):
        self.status = status if status is not None else {}
        self.messages = messages if messages is not None else []
        self.children = children if children is not None else []
        self.missing_messages = missing_messages

    async def get_session_status(self):
        return self.status

    async def list_messages(self, session_id):
        if self.missing_messages:
            raise OpenCodeClientError("not found", status=404)
        return self.messages

    async def list_session_children(self, session_id):
        return self.children


def test_opencode_status_helpers_are_flexible():
    assert is_opencode_status_active({"state": "busy"}) is True
    assert is_opencode_status_active({"running": True}) is True
    assert is_opencode_status_terminal_or_idle({"status": "completed"}) is True
    assert is_opencode_status_terminal_or_idle("idle") is True


@pytest.mark.asyncio
async def test_resolve_run_state_uses_status_active():
    state = await resolve_opencode_run_state(
        _RunStateClient(
            status={"sessions": {"ses-1": {"state": "running"}}},
            messages=[{"id": "u-1", "role": "user", "parts": [{"type": "text", "text": "hi"}]}],
        ),
        "ses-1",
    )

    assert state.exists is True
    assert state.active is True
    assert state.status == "running"
    assert state.reason == "opencode_status_active"


@pytest.mark.asyncio
async def test_resolve_run_state_final_assistant_is_inactive_and_visible_only():
    state = await resolve_opencode_run_state(
        _RunStateClient(
            status={"sessions": {"ses-1": {"state": "idle"}}},
            messages=[
                {"id": "u-1", "role": "user", "parts": [{"type": "text", "text": "hi"}]},
                {
                    "id": "a-1",
                    "role": "assistant",
                    "parts": [{"type": "reasoning", "text": "hidden"}, {"type": "text", "text": "visible final"}],
                    "finish_reason": "stop",
                },
            ],
        ),
        "ses-1",
    )

    assert state.exists is True
    assert state.active is False
    assert state.has_final_assistant is True
    assert state.assistant_message_ids == ["a-1"]
    assert state.last_message_id == "a-1"
    assert state.reason == "final_assistant_message"


@pytest.mark.asyncio
async def test_resolve_run_state_marks_active_child_session():
    state = await resolve_opencode_run_state(
        _RunStateClient(
            status={"sessions": {"parent": {"state": "idle"}, "child": {"state": "streaming"}}},
            messages=[],
            children=[{"id": "child"}],
        ),
        "parent",
    )

    assert state.active is True
    assert state.child_sessions == ["child"]
    assert state.active_child_sessions == ["child"]
    assert state.reason == "active_child_session"


@pytest.mark.asyncio
async def test_resolve_run_state_missing_session():
    state = await resolve_opencode_run_state(
        _RunStateClient(status={"sessions": {}}, missing_messages=True),
        "missing",
    )

    assert state.exists is False
    assert state.active is False
    assert state.reason == "opencode_session_missing"


@pytest.mark.asyncio
async def test_resolve_run_state_missing_opencode_session_id():
    state = await resolve_opencode_run_state(_RunStateClient(), "")

    assert state.exists is False
    assert state.active is False
    assert state.reason == "missing_opencode_session_id"
