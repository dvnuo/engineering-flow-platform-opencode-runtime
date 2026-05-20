import asyncio

import pytest

from efp_opencode_adapter.event_bus import EventBus


@pytest.mark.asyncio
async def test_event_bus_replay_by_session_id():
    bus = EventBus(replay_limit=10, replay_ttl_seconds=60)
    await bus.publish({"type": "tool.started", "session_id": "s1", "request_id": "r1", "data": {"tool": "bash"}})
    await bus.publish({"type": "tool.completed", "session_id": "s1", "request_id": "r1", "data": {"tool": "bash"}})
    await bus.publish({"type": "tool.started", "session_id": "other", "request_id": "r2"})

    replayed = bus.recent_events(session_id="s1")

    assert [event["type"] for event in replayed] == ["tool.started", "tool.completed"]
    assert all(event["metadata"]["replayed"] is True for event in replayed)
    assert all(event["data"]["replayed"] is True for event in replayed)


@pytest.mark.asyncio
async def test_event_bus_replay_by_request_id_dedupes_session_and_request():
    bus = EventBus(replay_limit=10, replay_ttl_seconds=60)
    await bus.publish({"type": "message.delta", "session_id": "s1", "request_id": "r1", "data": {"delta": "hi"}})

    replayed = bus.recent_events(session_id="s1", request_id="r1")

    assert len(replayed) == 1
    assert replayed[0]["type"] == "message.delta"


@pytest.mark.asyncio
async def test_event_bus_replay_with_request_id_isolates_from_same_session():
    bus = EventBus(replay_limit=10, replay_ttl_seconds=60)
    await bus.publish({"type": "session.status", "session_id": "s1", "opencode_session_id": "oc-1", "data": {"raw_type": "session.status"}})
    await bus.publish({"type": "message.delta", "session_id": "s1", "request_id": "r1", "data": {"delta": "one"}})
    await bus.publish({"type": "message.delta", "session_id": "s1", "request_id": "r2", "data": {"delta": "two"}})

    replayed = bus.recent_events(session_id="s1", request_id="r1")

    assert [event["request_id"] for event in replayed] == ["r1"]
    assert [event["data"]["delta"] for event in replayed] == ["one"]


@pytest.mark.asyncio
async def test_event_bus_replay_by_session_id_includes_session_level_events_without_request_id():
    bus = EventBus(replay_limit=10, replay_ttl_seconds=60)
    await bus.publish({"type": "session.status", "session_id": "s1", "opencode_session_id": "oc-1", "raw_type": "session.status", "data": {"raw_type": "session.status"}})
    await bus.publish({"type": "opencode.message.part.updated", "session_id": "s1", "opencode_session_id": "oc-1", "raw_type": "message.part.updated", "data": {"raw_type": "message.part.updated"}})
    await bus.publish({"type": "session.updated", "session_id": "s1", "opencode_session_id": "oc-1", "raw_type": "session.idle", "data": {"raw_type": "session.idle"}})

    replayed = bus.recent_events(session_id="s1")

    assert [event["data"]["raw_type"] for event in replayed] == ["session.status", "message.part.updated", "session.idle"]
    assert all(event["session_id"] == "s1" for event in replayed)
    assert all(event["opencode_session_id"] == "oc-1" for event in replayed)


@pytest.mark.asyncio
async def test_event_bus_replay_type_filtering():
    bus = EventBus(replay_limit=10, replay_ttl_seconds=60)
    await bus.publish({"type": "tool.started", "session_id": "s1", "request_id": "r1"})
    await bus.publish({"type": "provider.retry", "session_id": "s1", "request_id": "r1"})

    replayed = bus.recent_events(session_id="s1", types={"provider.retry"})

    assert [event["type"] for event in replayed] == ["provider.retry"]


@pytest.mark.asyncio
async def test_event_bus_replay_limit_behavior():
    bus = EventBus(replay_limit=2, replay_ttl_seconds=60)
    await bus.publish({"type": "one", "session_id": "s1"})
    await bus.publish({"type": "two", "session_id": "s1"})
    await bus.publish({"type": "three", "session_id": "s1"})

    replayed = bus.recent_events(session_id="s1")

    assert [event["type"] for event in replayed] == ["two", "three"]


@pytest.mark.asyncio
async def test_event_bus_replay_after_last_event_at():
    bus = EventBus(replay_limit=10, replay_ttl_seconds=60)
    await bus.publish({"type": "one", "session_id": "s1", "created_at": "2026-05-18T00:00:00+00:00"})
    await bus.publish({"type": "two", "session_id": "s1", "created_at": "2026-05-18T00:00:01+00:00"})
    await bus.publish({"type": "three", "session_id": "s1", "created_at": "2026-05-18T00:00:02+00:00"})

    replayed = bus.recent_events(session_id="s1", last_event_at="2026-05-18T00:00:01+00:00")

    assert [event["type"] for event in replayed] == ["three"]


@pytest.mark.asyncio
async def test_event_bus_replay_ignores_invalid_last_event_at():
    bus = EventBus(replay_limit=10, replay_ttl_seconds=60)
    await bus.publish({"type": "one", "session_id": "s1", "created_at": "2026-05-18T00:00:00+00:00"})
    await bus.publish({"type": "two", "session_id": "s1", "created_at": "2026-05-18T00:00:01+00:00"})

    replayed = bus.recent_events(session_id="s1", last_event_at="not-a-date")

    assert [event["type"] for event in replayed] == ["one", "two"]


@pytest.mark.asyncio
async def test_event_bus_replay_ttl_behavior(monkeypatch):
    now = 1000.0
    monkeypatch.setattr("efp_opencode_adapter.event_bus.time.time", lambda: now)
    bus = EventBus(replay_limit=10, replay_ttl_seconds=5)
    await bus.publish({"type": "old", "session_id": "s1"})
    now = 1006.0
    await bus.publish({"type": "new", "session_id": "s1"})

    replayed = bus.recent_events(session_id="s1")

    assert [event["type"] for event in replayed] == ["new"]


@pytest.mark.asyncio
async def test_event_bus_old_subscriber_behavior_unchanged():
    bus = EventBus(replay_limit=10, replay_ttl_seconds=60)
    sub = bus.subscribe({"session_id": "s1"})

    await bus.publish({"type": "tool.started", "session_id": "s1"})

    got = await asyncio.wait_for(sub.queue.get(), timeout=1)
    assert got["type"] == "tool.started"
    bus.unsubscribe(sub)
