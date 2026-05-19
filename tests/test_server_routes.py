from efp_opencode_adapter.app_keys import EVENT_BUS_KEY
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


def test_chat_stream_and_events_routes_still_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())

    routes = {(route.method, route.resource.canonical) for route in app.router.routes()}

    assert ("POST", "/api/chat") in routes
    assert ("POST", "/api/chat/stream") in routes
    assert ("GET", "/api/chat/runs") in routes
    assert ("GET", "/api/chat/runs/{request_id}") in routes
    assert ("GET", "/api/events") in routes
    assert ("GET", "/api/sessions/{session_id}/active-run") in routes


def test_event_bus_uses_replay_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_EVENT_REPLAY_LIMIT", "17")
    monkeypatch.setenv("EFP_EVENT_REPLAY_TTL_SECONDS", "23")

    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    bus = app[EVENT_BUS_KEY]

    assert bus.replay_limit == 17
    assert bus.replay_ttl_seconds == 23
