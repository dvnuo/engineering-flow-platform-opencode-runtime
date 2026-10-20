"""Which model answered: stored per assistant message, served with history and the chat payload."""
import pytest

from efp_opencode_adapter import copilot_auto_session as mod
from efp_opencode_adapter.app_keys import COPILOT_AUTO_SESSION_MANAGER_KEY
from efp_opencode_adapter.chat_api import _model_attribution
from efp_opencode_adapter.copilot_auto_session import AutoRouting, CopilotAutoSession, CopilotAutoSessionManager
from efp_opencode_adapter.copilot_plugin_auth import CopilotInternalToken
from efp_opencode_adapter.outbound_proxy import OutboundProxyConfig
from efp_opencode_adapter.sessions_api import _to_efp_messages, attach_canonical_assistant_attribution, normalize_canonical_messages
from efp_opencode_adapter.user_display_store import UserDisplayStore

MESSAGES = [
    {"info": {"id": "m1", "role": "user", "sessionID": "oc1"}, "parts": [{"type": "text", "text": "hi"}]},
    {"info": {"id": "m2", "role": "assistant", "sessionID": "oc1", "modelID": "auto"}, "parts": [{"type": "text", "text": "hello"}]},
]


def test_chat_model_attribution_follows_the_last_auto_routing():
    manager = CopilotAutoSessionManager()
    app = {COPILOT_AUTO_SESSION_MANAGER_KEY: manager}

    assert _model_attribution(app, "github-copilot/gpt-6-sol") == {"model": "gpt-6-sol"}
    assert _model_attribution(app, "gpt-6-luna") == {"model": "gpt-6-luna"}
    assert _model_attribution(app, None) == {}
    assert _model_attribution(app, "github-copilot/auto") == {
        "requested_model": "auto",
        "model": "gpt-6-sol",
        "model_fallback_reason": "no auto routing recorded for this request",
    }

    manager.last_routing = AutoRouting(model="gpt-6.1-sol", session_token="sess")
    assert _model_attribution(app, "auto") == {"requested_model": "auto", "model": "gpt-6.1-sol"}

    manager.last_routing = AutoRouting(model="gpt-6-sol", session_token=None, fallback_reason="auto session unavailable: HTTP 404")
    routed = _model_attribution(app, "auto")
    assert routed["model"] == "gpt-6-sol"
    assert routed["model_fallback_reason"].startswith("auto session unavailable")


@pytest.mark.asyncio
async def test_manager_remembers_its_last_routing(monkeypatch):
    manager = CopilotAutoSessionManager()

    async def fake_open(internal_token, proxy_config):
        return CopilotAutoSession("sess", 4102444800, ("gpt-6-luna",), {})

    monkeypatch.setattr(manager, "_open", fake_open)
    token = CopilotInternalToken(token="tid=A", expires_at=4102444800, api_base_url="https://api.example")
    assert manager.last_routing is None

    routing = await manager.route(token, proxy_config=OutboundProxyConfig(proxy_url=None))

    assert routing.model == "gpt-6-luna"
    assert manager.last_routing is routing


def test_history_messages_carry_the_stored_assistant_model(tmp_path):
    store = UserDisplayStore(tmp_path / "user_display_messages.json")
    store.put_assistant_attribution(
        portal_session_id="s1",
        opencode_session_id="oc1",
        opencode_message_id="m2",
        attribution={"model": "gpt-6.1-sol", "requested_model": "auto", "model_fallback_reason": ""},
    )

    efp = _to_efp_messages(MESSAGES, display_store=store, portal_session_id="s1", opencode_session_id="oc1")
    assert efp[1]["model"] == "gpt-6.1-sol"
    assert efp[1]["requested_model"] == "auto"
    assert "model_fallback_reason" not in efp[1]
    assert efp[1]["metadata"]["model"] == "gpt-6.1-sol"
    assert "model" not in efp[0]

    canonical = attach_canonical_assistant_attribution(
        normalize_canonical_messages(MESSAGES),
        display_store=store,
        portal_session_id="s1",
        opencode_session_id="oc1",
    )
    assert canonical[1]["model"] == "gpt-6.1-sol"
    assert canonical[1]["requested_model"] == "auto"
    assert "model" not in canonical[0]

    # Survives a reload of the store, like the user display records.
    reloaded = UserDisplayStore(tmp_path / "user_display_messages.json")
    record = reloaded.get_message("oc1", "m2", "s1")
    assert record["role"] == "assistant"
    assert record["metadata"] == {"model": "gpt-6.1-sol", "requested_model": "auto"}


def test_messages_without_a_stored_attribution_are_left_alone(tmp_path):
    store = UserDisplayStore(tmp_path / "user_display_messages.json")

    efp = _to_efp_messages(MESSAGES, display_store=store, portal_session_id="s1", opencode_session_id="oc1")
    canonical = attach_canonical_assistant_attribution(
        normalize_canonical_messages(MESSAGES), display_store=store, portal_session_id="s1", opencode_session_id="oc1"
    )

    assert "model" not in efp[1]
    assert "model" not in canonical[1]
    assert attach_canonical_assistant_attribution([], display_store=None, portal_session_id="s1", opencode_session_id="oc1") == []
