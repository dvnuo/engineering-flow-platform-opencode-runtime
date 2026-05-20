import pytest

from efp_opencode_adapter.chat_run_store import ChatRunStore
from efp_opencode_adapter.chat_run_validation import validate_chat_run_against_opencode
from efp_opencode_adapter.opencode_client import OpenCodeClientError


class _ValidationClient:
    def __init__(self, *, status=None, messages=None, children=None, missing=False):
        self.status = status if status is not None else {}
        self.messages = messages if messages is not None else []
        self.children = children if children is not None else []
        self.missing = missing

    async def get_session_status(self):
        return self.status

    async def list_messages(self, session_id):
        if self.missing:
            raise OpenCodeClientError("not found", status=404)
        return self.messages

    async def list_session_children(self, session_id):
        return self.children


class _EventBus:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)


@pytest.mark.asyncio
async def test_validate_chat_run_active_uses_opencode_source(tmp_path):
    store = ChatRunStore(tmp_path / "chat_runs.json")
    record = store.start_run(request_id="req-1", portal_session_id="portal-1", opencode_session_id="ses-1", status="running")

    public = await validate_chat_run_against_opencode(
        store=store,
        client=_ValidationClient(status={"sessions": {"ses-1": {"state": "running"}}}),
        record=record,
    )

    assert public["request_id"] == "req-1"
    assert public["opencode_active"] is True
    assert public["source_of_truth"] == "opencode"
    assert store.get("req-1").metadata["opencode_active"] is True


@pytest.mark.asyncio
async def test_validate_chat_run_missing_marks_stale_and_publishes(tmp_path):
    store = ChatRunStore(tmp_path / "chat_runs.json")
    record = store.start_run(request_id="req-1", portal_session_id="portal-1", opencode_session_id="ses-missing", status="running")
    bus = _EventBus()

    public = await validate_chat_run_against_opencode(
        store=store,
        client=_ValidationClient(status={"sessions": {}}, missing=True),
        record=record,
        event_bus=bus,
    )

    assert public is None
    assert store.get("req-1").status == "stale"
    assert [event["type"] for event in bus.events] == ["chat.run.stale", "opencode.session.missing"]


@pytest.mark.asyncio
async def test_validate_chat_run_idle_with_final_assistant_completes_projection(tmp_path):
    store = ChatRunStore(tmp_path / "chat_runs.json")
    record = store.start_run(request_id="req-1", portal_session_id="portal-1", opencode_session_id="ses-1", status="running")

    public = await validate_chat_run_against_opencode(
        store=store,
        client=_ValidationClient(
            status={"sessions": {"ses-1": {"state": "idle"}}},
            messages=[{"id": "a-1", "role": "assistant", "parts": [{"type": "text", "text": "done"}], "finish_reason": "stop"}],
        ),
        record=record,
    )

    assert public["status"] == "completed"
    assert public["opencode_active"] is False
    assert public["last_response_text"] == "done"
    assert store.active_for_session("portal-1") is None


@pytest.mark.asyncio
async def test_validate_chat_run_idle_without_final_marks_stale(tmp_path):
    store = ChatRunStore(tmp_path / "chat_runs.json")
    record = store.start_run(request_id="req-1", portal_session_id="portal-1", opencode_session_id="ses-1", status="running")

    public = await validate_chat_run_against_opencode(
        store=store,
        client=_ValidationClient(status={"sessions": {"ses-1": {"state": "idle"}}}, messages=[]),
        record=record,
    )

    assert public is None
    assert store.get("req-1").status == "stale"
    assert store.active_for_session("portal-1") is None


@pytest.mark.asyncio
async def test_validate_chat_run_child_active_root_idle_does_not_block_root(tmp_path):
    store = ChatRunStore(tmp_path / "chat_runs.json")
    record = store.start_run(request_id="req-1", portal_session_id="portal-1", opencode_session_id="root", status="running")

    public = await validate_chat_run_against_opencode(
        store=store,
        client=_ValidationClient(
            status={"sessions": {"root": {"type": "idle"}, "child": {"type": "busy"}}},
            messages=[],
            children=[{"id": "child"}],
        ),
        record=record,
    )

    assert public is None
    stale = store.get("req-1")
    assert stale.status == "stale"
    assert stale.metadata["opencode_active"] is False
    assert stale.metadata["opencode_active_child_sessions"] == ["child"]
    assert stale.metadata["validation_reason"] == "active_child_session_non_blocking"
