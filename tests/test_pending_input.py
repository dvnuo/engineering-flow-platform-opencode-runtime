"""What a session is blocked on, and the session-scoped API over it.

Portal draws its approval card from a live `permission.requested` event, but a
refresh or a dropped socket loses that event and leaves the run blocked with
nothing on screen. Portal therefore also polls
`/api/sessions/{id}/pending-input`. OpenCode keeps no record of an unanswered
permission -- `GET /session/{id}` does not report one -- so the adapter
reconstructs it from the event stream.
"""
import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.app_keys import EVENT_BUS_KEY, SESSION_STORE_KEY
from efp_opencode_adapter.event_bridge import OpenCodeEventBridge
from efp_opencode_adapter.event_bus import EventBus
from efp_opencode_adapter.opencode_client import OpenCodeClientError
from efp_opencode_adapter.pending_input import PendingInputStore
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.session_store import SessionRecord, SessionStore
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.state import ensure_state_dirs
from efp_opencode_adapter.task_store import TaskStore
from test_t06_helpers import FakeOpenCodeClient


class _BridgeClient:
    async def health(self):
        return {"healthy": True}

    async def event_stream(self, **kwargs):
        if False:
            yield {}


def _bridge():
    settings = Settings.from_env()
    paths = ensure_state_dirs(settings)
    session_store = SessionStore(paths.sessions_dir)
    session_store.upsert(SessionRecord("portal-1", "oc-1", "t", None, None, "a", "a", "", 0))
    bus = EventBus()
    store = PendingInputStore()
    bus.add_observer(store.observe_event)
    bridge = OpenCodeEventBridge(settings, _BridgeClient(), bus, session_store, TaskStore(paths.tasks_dir))
    return bridge, store


def _permission_event(session_id="portal-1", permission_id="perm-1", tool="bash", **extra):
    request = {
        "request_id": permission_id,
        "id": permission_id,
        "permission_id": permission_id,
        "tool": tool,
        "tool_id": tool,
        "tool_name": tool,
        "title": "Run git push",
        "args": "git push origin main",
        "risk_level": "high",
    }
    event = {
        "type": "permission.requested",
        "legacy_type": "permission_request",
        "session_id": session_id,
        "opencode_session_id": "oc-1",
        "request_id": "req-1",
        "permission_id": permission_id,
        "data": {"permission_request": request},
    }
    event.update(extra)
    return event


# ------------------------------------------------------------------ the store


def test_a_permission_request_becomes_pending():
    store = PendingInputStore()

    store.observe_event(_permission_event())

    snapshot = store.snapshot("portal-1")
    assert snapshot["permission_request"]["tool"] == "bash"
    assert snapshot["permission_request"]["request_id"] == "perm-1"
    assert snapshot["last_execution_id"] == "req-1"


def test_a_question_is_never_reported_as_answerable():
    # OpenCode 1.14.x has no question-response route, so a card offering to
    # answer one would have nowhere to send it.
    store = PendingInputStore()
    store.observe_event(_permission_event())

    assert store.snapshot("portal-1")["question_request"] is None


def test_an_unknown_session_reads_as_not_blocked():
    assert PendingInputStore().snapshot("nobody") == {
        "session_id": "nobody",
        "question_request": None,
        "permission_request": None,
        "last_execution_id": None,
    }


def test_resolution_clears_the_pending_request():
    store = PendingInputStore()
    store.observe_event(_permission_event())

    store.observe_event(
        {"type": "permission.resolved", "session_id": "portal-1", "permission_id": "perm-1", "data": {}}
    )

    assert store.snapshot("portal-1")["permission_request"] is None


def test_resolving_an_older_permission_leaves_a_newer_one_waiting():
    # Events can arrive out of order. Clearing on a mismatched id would drop a
    # request that is still blocking the run.
    store = PendingInputStore()
    store.observe_event(_permission_event(permission_id="perm-2"))

    store.observe_event(
        {"type": "permission.resolved", "session_id": "portal-1", "permission_id": "perm-1", "data": {}}
    )

    assert store.snapshot("portal-1")["permission_request"]["request_id"] == "perm-2"


@pytest.mark.parametrize("terminal", ["chat.completed", "chat.failed"])
def test_the_run_ending_clears_the_pending_request(terminal):
    # Once the turn is over OpenCode has dropped the waiter, so the card can no
    # longer be answered and must not linger.
    store = PendingInputStore()
    store.observe_event(_permission_event())

    store.observe_event({"type": terminal, "session_id": "portal-1", "data": {}})

    assert store.snapshot("portal-1")["permission_request"] is None


def test_the_terminal_event_chat_actually_emits_clears_it():
    # The test above builds the event by hand, so it would keep passing if
    # chat_api renamed the field the store matches on. This one is built by the
    # same helper the chat path uses.
    from efp_opencode_adapter.chat_api import _event_payload

    store = PendingInputStore()
    store.observe_event(_permission_event())

    store.observe_event(
        _event_payload(
            "chat.completed",
            session_id="portal-1",
            request_id="req-1",
            opencode_session_id="oc-1",
            state="success",
            summary="done",
            data={"completion_state": "completed"},
            trace_context={},
        )
    )

    assert store.snapshot("portal-1")["permission_request"] is None


def test_a_replayed_event_does_not_resurrect_an_answered_card():
    # Reconnecting clients get history re-sent with metadata.replayed set.
    store = PendingInputStore()

    store.observe_event(_permission_event(metadata={"replayed": True}))

    assert store.snapshot("portal-1")["permission_request"] is None


def test_a_request_without_an_id_is_ignored():
    store = PendingInputStore()

    store.observe_event(
        {"type": "permission.requested", "session_id": "portal-1", "data": {"permission_request": {"tool": "bash"}}}
    )

    assert store.snapshot("portal-1")["permission_request"] is None


def test_an_event_without_the_assembled_request_is_ignored():
    # The event carries the permission scattered across sibling keys as well;
    # only the assembled object is usable, so a bare event is not recorded.
    store = PendingInputStore()

    store.observe_event({"type": "permission.requested", "session_id": "portal-1", "permission_id": "p", "data": {}})

    assert store.snapshot("portal-1")["permission_request"] is None


def test_a_malformed_event_cannot_break_the_bus():
    store = PendingInputStore()

    store.observe_event(None)
    store.observe_event({"type": "permission.requested", "session_id": "s", "data": "not-a-dict"})

    assert store.snapshot("s")["permission_request"] is None


def test_tracked_sessions_are_bounded():
    # A leak stop: an abandoned permission would otherwise be held forever.
    store = PendingInputStore(max_sessions=2)

    for index in range(5):
        store.observe_event(_permission_event(session_id=f"s{index}"))

    assert [store.snapshot(f"s{i}")["permission_request"] is not None for i in range(5)] == [
        False,
        False,
        False,
        True,
        True,
    ]


def test_a_claim_is_exclusive():
    store = PendingInputStore()
    store.observe_event(_permission_event())

    assert store.claim("portal-1", "perm-1") is True
    assert store.claim("portal-1", "perm-1") is False


def test_a_released_claim_can_be_retaken():
    store = PendingInputStore()
    store.observe_event(_permission_event())
    store.claim("portal-1", "perm-1")

    store.release("portal-1", "perm-1")

    assert store.claim("portal-1", "perm-1") is True


def test_claiming_the_wrong_id_fails():
    store = PendingInputStore()
    store.observe_event(_permission_event())

    assert store.claim("portal-1", "other") is False


def test_a_repeated_event_for_the_same_permission_keeps_the_claim():
    # OpenCode's event is `permission.updated`, and nothing stops it arriving
    # twice for one permission. Re-recording with a fresh claim would release a
    # response already in flight and let the same tool call be approved twice.
    store = PendingInputStore()
    store.observe_event(_permission_event())
    store.claim("portal-1", "perm-1")

    store.observe_event(_permission_event())

    assert store.claim("portal-1", "perm-1") is False


def test_a_different_permission_starts_unclaimed():
    # The claim belongs to the request, not the session: a genuinely new block
    # must be answerable.
    store = PendingInputStore()
    store.observe_event(_permission_event())
    store.claim("portal-1", "perm-1")

    store.observe_event(_permission_event(permission_id="perm-2"))

    assert store.claim("portal-1", "perm-2") is True


# ------------------------------------------------------- the bridge feeds it


@pytest.mark.asyncio
async def test_a_real_opencode_permission_event_becomes_pending():
    bridge, store = _bridge()

    await bridge.publish_raw_event(
        {
            "payload": {
                "type": "permission.updated",
                "properties": {
                    "sessionID": "oc-1",
                    "id": "perm-9",
                    "title": "Run git push",
                    "tool": "bash",
                    "command": "git push origin main",
                },
            }
        }
    )

    request = store.snapshot("portal-1")["permission_request"]
    assert request is not None
    assert request["request_id"] == "perm-9"
    assert request["tool"] == "bash"
    assert request["title"] == "Run git push"


@pytest.mark.asyncio
async def test_the_event_carries_the_card_payload_portal_reads():
    # Portal's live card is built from data["permission_request"]; without it
    # the event arrives with the permission scattered and no card renders.
    bridge, _store = _bridge()

    event = await bridge.publish_raw_event(
        {"payload": {"type": "permission.asked", "properties": {"sessionID": "oc-1", "requestID": "perm-1", "tool": "bash"}}}
    )

    assert event["data"]["permission_request"]["request_id"] == "perm-1"


@pytest.mark.asyncio
async def test_a_resolved_event_carries_no_card_payload():
    bridge, _store = _bridge()

    event = await bridge.publish_raw_event(
        {
            "payload": {
                "type": "permission.updated",
                "properties": {"sessionID": "oc-1", "id": "perm-1", "status": "approved", "tool": "bash"},
            }
        }
    )

    assert event["type"] == "permission.resolved"
    assert "permission_request" not in event["data"]


@pytest.mark.asyncio
async def test_the_permission_title_stands_in_when_there_is_no_tool_input():
    # Portal shows args in the card's preview block; an empty block tells the
    # member nothing about what they are approving.
    bridge, store = _bridge()

    await bridge.publish_raw_event(
        {"payload": {"type": "permission.asked", "properties": {"sessionID": "oc-1", "id": "perm-1", "title": "Edit README.md"}}}
    )

    assert store.snapshot("portal-1")["permission_request"]["args"] == "Edit README.md"


# -------------------------------------------------------------- the endpoints


class _TrackingClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.permission_calls = []
        self.fail_with = None

    async def respond_permission(self, session_id, permission_id, payload):
        if self.fail_with is not None:
            raise self.fail_with
        self.permission_calls.append((session_id, permission_id, payload))
        return {"success": True}


async def _serve():
    client = _TrackingClient()
    app = create_app(Settings.from_env(), opencode_client=client)
    app[SESSION_STORE_KEY].upsert(SessionRecord("portal-1", "oc-1", "t", None, None, "a", "a", "", 0))
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    return app, test_client, client


async def _block(app, **kwargs):
    await app[EVENT_BUS_KEY].publish(_permission_event(**kwargs))


async def test_pending_input_reports_nothing_for_an_idle_session():
    app, client, _fake = await _serve()

    resp = await client.get("/api/sessions/portal-1/pending-input")
    body = await resp.json()
    await client.close()

    assert resp.status == 200
    assert body == {
        "session_id": "portal-1",
        "question_request": None,
        "permission_request": None,
        "last_execution_id": None,
    }


async def test_pending_input_reports_the_blocking_permission():
    app, client, _fake = await _serve()
    await _block(app)

    resp = await client.get("/api/sessions/portal-1/pending-input")
    body = await resp.json()
    await client.close()

    assert body["permission_request"]["tool"] == "bash"


@pytest.mark.parametrize(
    ("decision", "always", "expected"),
    [
        ("approve", False, "once"),
        ("approve", True, "always"),
        ("allow", False, "once"),
        ("deny", False, "reject"),
        ("reject", True, "reject"),
    ],
)
async def test_a_decision_maps_onto_opencodes_vocabulary(decision, always, expected):
    app, client, fake = await _serve()
    await _block(app)

    resp = await client.post(
        "/api/sessions/portal-1/permission/respond",
        json={"request_id": "perm-1", "decision": decision, "always": always},
    )
    await client.close()

    assert resp.status == 202
    assert fake.permission_calls == [("oc-1", "perm-1", {"response": expected})]


@pytest.mark.parametrize(
    ("always", "expected"),
    [
        (True, "always"),
        ("true", "always"),
        ("1", "always"),
        (1, "always"),
        # `bool("false")` is True. Reading these as a yes would persist a
        # standing approval for the tool that nobody asked for -- so anything
        # not recognisably true falls back to a single-use approval.
        ("false", "once"),
        ("0", "once"),
        (0, "once"),
        ("", "once"),
        (None, "once"),
        ("maybe", "once"),
        ({"nested": True}, "once"),
    ],
)
async def test_a_standing_approval_needs_an_unambiguous_yes(always, expected):
    app, client, fake = await _serve()
    await _block(app)

    resp = await client.post(
        "/api/sessions/portal-1/permission/respond",
        json={"request_id": "perm-1", "decision": "approve", "always": always},
    )
    await client.close()

    assert resp.status == 202
    assert fake.permission_calls == [("oc-1", "perm-1", {"response": expected})]


async def test_an_omitted_always_is_a_single_use_approval():
    app, client, fake = await _serve()
    await _block(app)

    await client.post(
        "/api/sessions/portal-1/permission/respond", json={"request_id": "perm-1", "decision": "approve"}
    )
    await client.close()

    assert fake.permission_calls == [("oc-1", "perm-1", {"response": "once"})]


async def test_answering_clears_the_pending_request():
    app, client, _fake = await _serve()
    await _block(app)

    await client.post(
        "/api/sessions/portal-1/permission/respond", json={"request_id": "perm-1", "decision": "approve"}
    )
    body = await (await client.get("/api/sessions/portal-1/pending-input")).json()
    await client.close()

    assert body["permission_request"] is None


async def test_a_second_submission_cannot_approve_the_same_call_twice():
    # Two clicks landing together would otherwise both pass the id check and
    # run the approved tool twice. Sending them back to back does not reproduce
    # it -- the first finishes and clears the pending request before the second
    # is read -- so the first is held inside the OpenCode call, which is the
    # only window in which both can be in flight at once.
    app, client, fake = await _serve()
    await _block(app)

    in_flight = asyncio.Event()
    release = asyncio.Event()
    respond = fake.respond_permission
    held = False

    async def _held(session_id, permission_id, payload):
        # Only the first call waits. A second one has to be able to run to
        # completion, or a regression here would deadlock the test instead of
        # failing it.
        nonlocal held
        if not held:
            held = True
            in_flight.set()
            await release.wait()
        return await respond(session_id, permission_id, payload)

    fake.respond_permission = _held

    body = {"request_id": "perm-1", "decision": "approve"}
    first = asyncio.ensure_future(client.post("/api/sessions/portal-1/permission/respond", json=body))
    await asyncio.wait_for(in_flight.wait(), timeout=2)
    second = await client.post("/api/sessions/portal-1/permission/respond", json=body)
    release.set()
    first_response = await asyncio.wait_for(first, timeout=2)
    await client.close()

    assert first_response.status == 202
    assert second.status == 409
    assert len(fake.permission_calls) == 1


async def test_answering_a_session_that_is_not_blocked_is_a_conflict():
    _app, client, _fake = await _serve()

    resp = await client.post(
        "/api/sessions/portal-1/permission/respond", json={"request_id": "perm-1", "decision": "approve"}
    )
    await client.close()

    assert resp.status == 409


async def test_a_stale_request_id_is_rejected():
    # A card left open from a previous block must not answer the current one.
    app, client, fake = await _serve()
    await _block(app, permission_id="perm-2")

    resp = await client.post(
        "/api/sessions/portal-1/permission/respond", json={"request_id": "perm-1", "decision": "approve"}
    )
    await client.close()

    assert resp.status == 409
    assert fake.permission_calls == []


async def test_an_unusable_decision_is_a_bad_request_not_a_denial():
    # Silently rejecting on a typo would deny a tool the member meant to allow.
    app, client, fake = await _serve()
    await _block(app)

    resp = await client.post(
        "/api/sessions/portal-1/permission/respond", json={"request_id": "perm-1", "decision": "maybe"}
    )
    await client.close()

    assert resp.status == 400
    assert fake.permission_calls == []


async def test_a_missing_decision_is_a_bad_request():
    app, client, _fake = await _serve()
    await _block(app)

    resp = await client.post("/api/sessions/portal-1/permission/respond", json={"request_id": "perm-1"})
    await client.close()

    assert resp.status == 400


async def test_a_non_json_body_is_a_bad_request():
    app, client, _fake = await _serve()
    await _block(app)

    resp = await client.post(
        "/api/sessions/portal-1/permission/respond", data="not json", headers={"Content-Type": "application/json"}
    )
    await client.close()

    assert resp.status == 400


async def test_an_opencode_failure_leaves_the_request_answerable():
    # The permission is still waiting, so the member must be able to try again
    # rather than face a card whose buttons are now inert.
    app, client, fake = await _serve()
    await _block(app)
    fake.fail_with = OpenCodeClientError("boom", status=500)

    failed = await client.post(
        "/api/sessions/portal-1/permission/respond", json={"request_id": "perm-1", "decision": "approve"}
    )
    fake.fail_with = None
    retried = await client.post(
        "/api/sessions/portal-1/permission/respond", json={"request_id": "perm-1", "decision": "approve"}
    )
    await client.close()

    assert failed.status == 502
    assert retried.status == 202
    assert fake.permission_calls == [("oc-1", "perm-1", {"response": "once"})]


async def test_answering_announces_the_resolution():
    # The card is drawn from the event stream too, so a resolution that is not
    # announced leaves it on screen in every other open tab.
    app, client, _fake = await _serve()
    ws = await client.ws_connect("/api/events?session_id=portal-1")
    await ws.receive_json()
    await _block(app)
    await ws.receive_json(timeout=2)

    await client.post(
        "/api/sessions/portal-1/permission/respond", json={"request_id": "perm-1", "decision": "approve"}
    )
    event = await ws.receive_json(timeout=2)
    await ws.close()
    await client.close()

    assert event["type"] == "permission_resolved"
    assert event["data"]["permission_id"] == "perm-1"


async def test_the_permission_id_endpoint_also_clears_the_pending_request():
    # The older `/api/permissions/{id}/respond` route is still there. It has to
    # leave the same state behind, or a card answered through it would come
    # back on the next poll.
    app, client, fake = await _serve()
    await _block(app)

    resp = await client.post(
        "/api/permissions/perm-1/respond",
        json={"decision": "allow", "session_id": "portal-1", "opencode_session_id": "oc-1"},
    )
    body = await (await client.get("/api/sessions/portal-1/pending-input")).json()
    await client.close()

    assert resp.status == 200
    assert body["permission_request"] is None


async def test_a_question_response_says_the_runtime_cannot_take_one():
    # A 404 would read as a broken deployment. OpenCode 1.14.x simply has no
    # question-response route.
    _app, client, _fake = await _serve()

    resp = await client.post("/api/sessions/portal-1/question/respond", json={"answers": ["yes"]})
    body = await resp.json()
    await client.close()

    assert resp.status == 501
    assert body["error"] == "question_response_unsupported"
