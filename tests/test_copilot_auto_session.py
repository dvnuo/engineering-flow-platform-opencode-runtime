"""Copilot "auto" through the adapter's loopback Copilot proxy.

OpenCode sends ``model: "auto"`` (or a tier id); the proxy routes the turn
through ``POST /auto`` the way VS Code's Auto picker does, rewrites the model
to the one Copilot picked and adds Copilot-Session-Token, and falls back to the
default model without the session when it cannot drive the pick or the route
fails.
"""

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter import copilot_auto_session as mod
from efp_opencode_adapter.app_keys import COPILOT_AUTO_SESSION_MANAGER_KEY, COPILOT_TOKEN_MANAGER_KEY, SETTINGS_KEY
from efp_opencode_adapter.copilot_auto_catalog import (
    COPILOT_AUTO_MODEL_CONFIGS,
    COPILOT_MODEL_LIMITS,
    allowed_reasoning_efforts,
    auto_pick_refusal,
    auto_tier_for,
    clamp_reasoning_effort,
    copilot_model_limits,
    smallest_model_limits,
    static_model_limits,
)
from efp_opencode_adapter.copilot_auto_session import (
    AUTO_MODEL_IDS,
    AutoTurn,
    CopilotAutoDecision,
    CopilotAutoSessionError,
    CopilotAutoSessionManager,
    auto_turn_from_payload,
    is_auto_model,
    parse_auto_decision,
    parse_models_catalog,
)
from efp_opencode_adapter.copilot_plugin_auth import CopilotInternalToken
from efp_opencode_adapter.copilot_proxy import copilot_proxy_handler
from efp_opencode_adapter.opencode_config import build_opencode_config
from efp_opencode_adapter.outbound_proxy import OutboundProxyConfig
from efp_opencode_adapter.settings import Settings

FAR_FUTURE = 4102444800


def _entry(model_id, *, window, prompt, output, efforts=None, endpoints=("/responses",), tool_calls=True):
    entry = {
        "id": model_id,
        "name": model_id,
        "vendor": "OpenAI",
        "capabilities": {
            "family": model_id,
            "limits": {
                "max_context_window_tokens": window,
                "max_prompt_tokens": prompt,
                "max_output_tokens": output,
            },
            "supports": {"tool_calls": tool_calls},
        },
        "supported_endpoints": list(endpoints),
    }
    if efforts is not None:
        entry["capabilities"]["supports"]["reasoning_effort"] = list(efforts)
    return entry


# What the 2026-10-20 probe saw on a Business seat: /auto embeds OpenAI's
# published windows, GET /models reports the gateway's smaller ones.
LUNA_AUTO = _entry("gpt-6-luna", window=1000000, prompt=872000, output=128000, efforts=("none", "low", "medium", "high", "xhigh", "max"))
LUNA_CATALOG = _entry("gpt-6-luna", window=400000, prompt=272000, output=128000, efforts=("none", "low", "medium", "high", "xhigh", "max"))
MAI = _entry("mai-code-1.1-flash", window=256000, prompt=128000, output=128000, efforts=("low", "medium", "high"))
CLAUDE = _entry("claude-sonnet-5.5", window=200000, prompt=150000, output=50000, endpoints=("/chat/completions", "/messages"))


class StaticTokenManager:
    def __init__(self, api_base_url):
        self.token = CopilotInternalToken(token="tid=INTERNAL_TOKEN", expires_at=FAR_FUTURE, api_base_url=api_base_url)

    async def get_token(self):
        return self.token

    def status_snapshot(self):
        return {"credential_present": True, "token_cached": True, "expires_at_present": True}


async def _upstream(selected=LUNA_AUTO, route_status=200, catalog=(LUNA_CATALOG,)):
    calls = {"route": [], "models": 0, "chat": []}

    async def route_handler(request):
        calls["route"].append({"headers": dict(request.headers), "body": await request.json()})
        if route_status != 200:
            return web.Response(status=route_status, text="auto is not available for tid=INTERNAL_TOKEN")
        return web.json_response(
            {
                "session_token": f"sess-{len(calls['route'])}",
                "expires_at": FAR_FUTURE,
                "selected_model": dict(selected),
                "discounted_costs": {selected["id"]: 0.1},
                "multi_turn_mode": "anchor",
                "hydra_scores": {"x": 0.5},
            }
        )

    async def models_handler(request):
        calls["models"] += 1
        return web.json_response({"data": list(catalog)})

    async def chat_handler(request):
        calls["chat"].append({"path": request.path, "headers": dict(request.headers), "body": await request.text()})
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/auto", route_handler)
    app.router.add_get("/models", models_handler)
    app.router.add_route("*", "/{tail:.*}", chat_handler)
    server = TestServer(app)
    await server.start_server()
    return server, calls


def _proxy_client(settings, upstream_base):
    app = web.Application()
    app[SETTINGS_KEY] = settings
    app[COPILOT_TOKEN_MANAGER_KEY] = StaticTokenManager(upstream_base)
    app[COPILOT_AUTO_SESSION_MANAGER_KEY] = CopilotAutoSessionManager()
    app.router.add_route("*", "/api/internal/copilot/{tail:.*}", copilot_proxy_handler)
    return TestClient(TestServer(app))


async def _client(tmp_path, monkeypatch, upstream):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    client = _proxy_client(Settings.from_env(), str(upstream.make_url("/")).rstrip("/"))
    await client.start_server()
    return client


def _user(text, *extra_parts):
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}, *extra_parts]}


def _sent_models(calls):
    return [json.loads(call["body"])["model"] for call in calls["chat"]]


@pytest.mark.asyncio
async def test_auto_turn_is_routed_once_and_sent_as_the_picked_model_with_the_session_token(tmp_path, monkeypatch):
    upstream, calls = await _upstream()
    client = await _client(tmp_path, monkeypatch, upstream)
    turn = [_user("hi")]
    first = await client.post(
        "/api/internal/copilot/responses",
        json={"model": "auto", "input": turn, "stream": False, "reasoning": {"effort": "high"}},
    )
    # OpenCode's tool-call continuation of the same turn.
    continuation = turn + [{"type": "function_call", "name": "read", "arguments": "{}"}, {"type": "function_call_output", "output": "x"}]
    second = await client.post("/api/internal/copilot/responses", json={"model": "github-copilot/auto", "input": continuation})
    assert first.status == 200
    assert second.status == 200
    assert len(calls["route"]) == 1
    route = calls["route"][0]
    assert route["body"] == {"prompt": "hi", "tier": "balance"}
    assert route["headers"]["Authorization"] == "Bearer tid=INTERNAL_TOKEN"
    assert route["headers"]["Copilot-Integration-Id"] == "vscode-chat"
    assert route["headers"]["X-GitHub-Api-Version"] == "2026-08-01"
    assert route["headers"]["Accept"] == "application/json"
    assert calls["models"] == 1
    assert _sent_models(calls) == ["gpt-6-luna", "gpt-6-luna"]
    sent = json.loads(calls["chat"][0]["body"])
    assert sent["input"] == turn
    assert sent["stream"] is False
    assert sent["reasoning"] == {"effort": "high"}
    assert [call["path"] for call in calls["chat"]] == ["/responses", "/responses"]
    assert all(call["headers"]["Copilot-Session-Token"] == "sess-1" for call in calls["chat"])
    assert all(call["headers"]["Authorization"] == "Bearer tid=INTERNAL_TOKEN" for call in calls["chat"])
    manager = client.app[COPILOT_AUTO_SESSION_MANAGER_KEY]
    routing = manager.last_routing
    assert routing.model == "gpt-6-luna"
    assert routing.tier == "balance"
    assert routing.discount == 0.1
    assert routing.session_active is True
    # The gateway's catalog row says less than the /auto metadata: it wins.
    assert routing.limits == {"context": 400000, "prompt": 272000, "output": 128000}
    await client.close()
    await upstream.close()


@pytest.mark.asyncio
async def test_a_new_user_message_routes_again_with_the_tier_the_id_names(tmp_path, monkeypatch):
    upstream, calls = await _upstream(selected=MAI)
    client = await _client(tmp_path, monkeypatch, upstream)
    first_turn = [_user("hi")]
    await client.post("/api/internal/copilot/responses", json={"model": "auto-intelligence", "input": first_turn, "reasoning": {"effort": "max"}})
    second_turn = first_turn + [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "yo"}]},
        _user("now refactor it", {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}),
    ]
    await client.post("/api/internal/copilot/responses", json={"model": "github-copilot/auto-intelligence", "input": second_turn, "reasoning": {"effort": "max"}})
    assert [call["body"] for call in calls["route"]] == [
        {"prompt": "hi", "tier": "intelligence"},
        {"prompt": "now refactor it", "tier": "intelligence", "has_image": True},
    ]
    assert _sent_models(calls) == ["mai-code-1.1-flash", "mai-code-1.1-flash"]
    # MAI lists low/medium/high: the body's effort is clamped to what it accepts.
    assert [json.loads(call["body"])["reasoning"] for call in calls["chat"]] == [{"effort": "high"}, {"effort": "high"}]
    assert [call["headers"]["Copilot-Session-Token"] for call in calls["chat"]] == ["sess-1", "sess-2"]
    assert client.app[COPILOT_AUTO_SESSION_MANAGER_KEY].last_routing.tier == "intelligence"
    await client.close()
    await upstream.close()


@pytest.mark.asyncio
async def test_concrete_model_requests_pass_through_byte_for_byte(tmp_path, monkeypatch):
    upstream, calls = await _upstream()
    client = await _client(tmp_path, monkeypatch, upstream)
    body = b'{"model": "gpt-6-sol",  "input": "hi"}'
    response = await client.post("/api/internal/copilot/responses", data=body, headers={"Content-Type": "application/json"})
    not_json = await client.post("/api/internal/copilot/responses", data=b"model=auto")
    assert response.status == 200
    assert not_json.status == 200
    assert calls["route"] == []
    assert calls["chat"][0]["body"] == body.decode("utf-8")
    assert calls["chat"][1]["body"] == "model=auto"
    assert all("Copilot-Session-Token" not in call["headers"] for call in calls["chat"])
    await client.close()
    await upstream.close()


@pytest.mark.asyncio
async def test_auto_falls_back_to_the_default_model_when_the_route_is_refused(tmp_path, monkeypatch, caplog):
    upstream, calls = await _upstream(route_status=404)
    client = await _client(tmp_path, monkeypatch, upstream)
    with caplog.at_level("WARNING"):
        first = await client.post("/api/internal/copilot/responses", json={"model": "auto", "input": "hi"})
        same_turn = await client.post("/api/internal/copilot/responses", json={"model": "auto", "input": "hi"})
        next_turn = await client.post("/api/internal/copilot/responses", json={"model": "auto", "input": "again"})
    assert (first.status, same_turn.status, next_turn.status) == (200, 200, 200)
    # A failed route is remembered for the turn; the next turn tries again.
    assert len(calls["route"]) == 2
    assert _sent_models(calls) == ["gpt-6-sol", "gpt-6-sol", "gpt-6-sol"]
    assert all("Copilot-Session-Token" not in call["headers"] for call in calls["chat"])
    warnings = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "auto route unavailable" in warnings[0]
    assert "HTTP 404" in warnings[0]
    assert "INTERNAL_TOKEN" not in warnings[0]
    routing = client.app[COPILOT_AUTO_SESSION_MANAGER_KEY].last_routing
    assert routing.fallback_reason.startswith("auto route unavailable")
    assert routing.tier == "balance"
    assert routing.limits == {"context": 400000, "prompt": 272000, "output": 128000}
    await client.close()
    await upstream.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selected, fragment",
    [
        (CLAUDE, "not served on /responses"),
        (_entry("gpt-6-luna", window=400000, prompt=272000, output=128000, tool_calls=False), "does not support tool calls"),
        ({"id": "gpt-6-luna", "capabilities": {"type": "embeddings"}}, "not a chat model"),
        ({"id": "mystery-model"}, "no usable context metadata"),
    ],
)
async def test_auto_falls_back_when_the_pick_cannot_be_driven(tmp_path, monkeypatch, selected, fragment):
    upstream, calls = await _upstream(selected=selected, catalog=())
    client = await _client(tmp_path, monkeypatch, upstream)
    response = await client.post("/api/internal/copilot/responses", json={"model": "auto", "input": "hi"})
    assert response.status == 200
    assert _sent_models(calls) == ["gpt-6-sol"]
    assert "Copilot-Session-Token" not in calls["chat"][0]["headers"]
    assert fragment in client.app[COPILOT_AUTO_SESSION_MANAGER_KEY].last_routing.fallback_reason
    await client.close()
    await upstream.close()


@pytest.mark.asyncio
async def test_auto_falls_back_when_the_pick_cannot_hold_the_request(tmp_path, monkeypatch):
    upstream, calls = await _upstream(selected=MAI, catalog=())
    client = await _client(tmp_path, monkeypatch, upstream)
    # MAI's prompt cap is 128k tokens; a 600k-character request is ~150k.
    big = [_user("x" * 600000), _user("and now?")]
    response = await client.post("/api/internal/copilot/responses", json={"model": "auto", "input": big})
    assert response.status == 200
    assert _sent_models(calls) == ["gpt-6-sol"]
    reason = client.app[COPILOT_AUTO_SESSION_MANAGER_KEY].last_routing.fallback_reason
    assert reason.startswith("selected model mai-code-1.1-flash: prompt cap 128000 tokens is below the request's ~150")
    assert "Copilot-Session-Token" not in calls["chat"][0]["headers"]
    await client.close()
    await upstream.close()


@pytest.mark.asyncio
async def test_a_body_without_a_user_message_is_sent_as_the_default_model(tmp_path, monkeypatch):
    upstream, calls = await _upstream()
    client = await _client(tmp_path, monkeypatch, upstream)
    response = await client.post("/api/internal/copilot/responses", json={"model": "auto", "input": [{"type": "function_call_output", "output": "x"}]})
    assert response.status == 200
    assert calls["route"] == []
    assert _sent_models(calls) == ["gpt-6-sol"]
    assert client.app[COPILOT_AUTO_SESSION_MANAGER_KEY].last_routing.fallback_reason == "no prompt to route"
    await client.close()
    await upstream.close()


# --- pure helpers ----------------------------------------------------------


def test_auto_ids_and_tiers():
    assert AUTO_MODEL_IDS == ("auto", "auto-efficiency", "auto-intelligence")
    for auto_id in AUTO_MODEL_IDS:
        assert is_auto_model(auto_id)
        assert is_auto_model("github-copilot/" + auto_id.upper())
    assert not is_auto_model("gpt-6-sol")
    assert not is_auto_model("auto-fast")
    assert not is_auto_model(None)
    assert auto_tier_for("auto") == "balance"
    assert auto_tier_for("github-copilot/Auto-Efficiency") == "efficiency"
    assert auto_tier_for("gpt-6-luna") is None


def test_auto_turn_from_payload_reads_responses_and_chat_bodies():
    turn = auto_turn_from_payload({"input": "  hey "}, body_size=40)
    assert turn == AutoTurn(prompt="hey", conversation_key=turn.conversation_key, turn_index=1, history_chars=40)
    items = [
        _user("first question"),
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]},
        {"type": "function_call", "name": "read", "arguments": "{}"},
        {"type": "function_call_output", "output": "x"},
        {"role": "user", "content": "second question"},
    ]
    later = auto_turn_from_payload({"input": items}, body_size=999)
    assert later.prompt == "second question"
    assert later.turn_index == 2
    assert later.history_chars == 999
    assert later.has_image is False
    # The key follows the first user message, so it is stable across the session.
    assert later.conversation_key == auto_turn_from_payload({"input": [items[0]]}, body_size=1).conversation_key
    assert later.conversation_key != turn.conversation_key
    chat = auto_turn_from_payload(
        {"messages": [{"role": "system", "content": "be nice"}, {"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "x"}}]}]},
        body_size=10,
    )
    assert chat.prompt == "look"
    assert chat.has_image is True
    image_only = auto_turn_from_payload({"input": [{"role": "user", "content": [{"type": "input_image", "image_url": "x"}]}]}, body_size=10)
    assert image_only.prompt == "(image)"
    assert image_only.has_image is True
    assert auto_turn_from_payload({"input": [{"type": "function_call_output", "output": "x"}]}, body_size=10) is None
    assert auto_turn_from_payload({"input": "   "}, body_size=10) is None
    assert auto_turn_from_payload({}, body_size=10) is None


def test_parse_auto_decision_validates_the_answer():
    decision = parse_auto_decision(
        {"session_token": " s ", "expires_at": 10.0, "selected_model": {"id": " GPT-6-Luna ", "name": "GPT-6 Luna"}, "discounted_costs": {"gpt-6-luna": 0.1}},
        tier="balance",
    )
    assert decision == CopilotAutoDecision("gpt-6-luna", "s", 10, "balance", {"id": " GPT-6-Luna ", "name": "GPT-6 Luna"}, {"gpt-6-luna": 0.1})
    for bad in (
        {},
        {"session_token": "s"},
        {"session_token": "s", "expires_at": True},
        {"session_token": "s", "expires_at": 10},
        {"session_token": "s", "expires_at": 10, "selected_model": {"name": "x"}},
        "nope",
    ):
        with pytest.raises(CopilotAutoSessionError):
            parse_auto_decision(bad)
    assert parse_models_catalog({"data": [LUNA_CATALOG, {"id": " GPT-6-Sol "}, "junk", {"name": "x"}]}) == {"gpt-6-luna": LUNA_CATALOG, "gpt-6-sol": {"id": " GPT-6-Sol "}}
    with pytest.raises(CopilotAutoSessionError):
        parse_models_catalog({"models": []})


def test_model_limits_helpers_follow_the_native_runtime():
    assert copilot_model_limits(LUNA_CATALOG) == {"context": 400000, "prompt": 272000, "output": 128000}
    assert copilot_model_limits({"id": "x", "capabilities": {"limits": {"max_context_window_tokens": 300000, "max_output_tokens": 50000}}}) == {"context": 300000, "prompt": 250000, "output": 50000}
    assert copilot_model_limits({"id": "x", "capabilities": {"limits": {"max_context_window_tokens": 100000}}}) == {"context": 100000, "prompt": 75000, "output": 25000}
    assert copilot_model_limits({"id": "x", "capabilities": {"limits": {"max_context_window_tokens": "big"}}}) is None
    assert copilot_model_limits({"id": "x"}) is None
    assert static_model_limits("github-copilot/gpt-6-sol") == {"context": 400000, "prompt": 272000, "output": 128000}
    assert static_model_limits("mai-code-1.1-flash") is None
    assert smallest_model_limits((copilot_model_limits(LUNA_AUTO), None, copilot_model_limits(LUNA_CATALOG))) == copilot_model_limits(LUNA_CATALOG)
    assert smallest_model_limits(()) is None
    assert auto_pick_refusal("m", LUNA_AUTO, None) is None
    assert auto_pick_refusal("m", {"supported_endpoints": ["ws:/responses"]}) is None
    assert "not served on /responses" in auto_pick_refusal("m", CLAUDE)
    assert allowed_reasoning_efforts(None, MAI) == ("low", "medium", "high")
    assert allowed_reasoning_efforts({"capabilities": {"supports": {}}}) is None
    assert clamp_reasoning_effort("max", ("low", "medium", "high")) == "high"
    assert clamp_reasoning_effort("low", ("none", "medium")) == "medium"
    assert clamp_reasoning_effort("medium", ("none",)) == "medium"
    assert clamp_reasoning_effort("high", None) == "high"
    assert clamp_reasoning_effort(3, ("low",)) == 3


@pytest.mark.asyncio
async def test_manager_routes_again_inside_the_refresh_margin_and_per_token(monkeypatch):
    now = [1_000_000]
    routed = []
    manager = CopilotAutoSessionManager()

    async def fake_route(internal_token, proxy_config, *, prompt, tier, has_image):
        routed.append((internal_token.token, prompt, tier))
        return CopilotAutoDecision("gpt-6-luna", f"sess-{len(routed)}", now[0] + 1000, tier, dict(LUNA_AUTO), {})

    async def no_catalog(internal_token, proxy_config):
        return {}

    monkeypatch.setattr(manager, "_route", fake_route)
    monkeypatch.setattr(manager, "_catalog", no_catalog)
    monkeypatch.setattr(mod.time, "time", lambda: now[0])
    token = CopilotInternalToken(token="tid=A", expires_at=FAR_FUTURE, api_base_url="https://api.example")
    proxy = OutboundProxyConfig(proxy_url=None)
    turn = AutoTurn(prompt="hey", conversation_key="c1", turn_index=1, history_chars=10)
    first = await manager.route(token, proxy_config=proxy, turn=turn, tier="balance")
    assert first.model == "gpt-6-luna"
    assert first.limits == {"context": 400000, "prompt": 272000, "output": 128000}
    now[0] = first.expires_at - mod.AUTO_SESSION_REFRESH_MARGIN_SECONDS - 1
    assert await manager.route(token, proxy_config=proxy, turn=turn, tier="balance") is first
    now[0] = first.expires_at - mod.AUTO_SESSION_REFRESH_MARGIN_SECONDS + 1
    second = await manager.route(token, proxy_config=proxy, turn=turn, tier="balance")
    assert second.session_token == "sess-2"
    # Another tier, another turn, another token each route on their own.
    await manager.route(token, proxy_config=proxy, turn=turn, tier="efficiency")
    await manager.route(token, proxy_config=proxy, turn=AutoTurn(prompt="hey", conversation_key="c1", turn_index=2, history_chars=10), tier="balance")
    other = CopilotInternalToken(token="tid=B", expires_at=FAR_FUTURE, api_base_url="https://api.example")
    await manager.route(other, proxy_config=proxy, turn=turn, tier="balance")
    assert routed == [("tid=A", "hey", "balance"), ("tid=A", "hey", "balance"), ("tid=A", "hey", "efficiency"), ("tid=A", "hey", "balance"), ("tid=B", "hey", "balance")]
    manager.invalidate(token)
    await manager.route(token, proxy_config=proxy, turn=turn, tier="balance")
    assert len(routed) == 6


def test_opencode_config_declares_the_auto_ids_and_pins_the_gpt_6_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    cfg, _, _ = build_opencode_config(
        Settings.from_env(),
        {"llm": {"provider": "github_copilot", "model": "auto-efficiency", "api_key": "ghu_source"}},
    )
    assert cfg["agent"]["efp-main"]["model"] == "github-copilot/auto-efficiency"
    provider_cfg = cfg["provider"]["github-copilot"]
    assert provider_cfg["npm"] == "@ai-sdk/openai"
    models = provider_cfg["models"]
    assert set(models) == set(AUTO_MODEL_IDS) | set(COPILOT_MODEL_LIMITS)
    assert models["auto"] == COPILOT_AUTO_MODEL_CONFIGS["auto"]
    assert models["auto"]["limit"] == {"context": 400000, "output": 128000}
    assert models["auto-intelligence"]["name"] == "Auto (Intelligence)"
    assert models["gpt-6.1-sol"] == {"limit": {"context": 400000, "output": 128000}}
