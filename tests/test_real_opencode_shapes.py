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

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient
from efp_opencode_adapter.usage_tracker import UsageTracker


class ListPayloadOpenCodeClient(FakeOpenCodeClient):
    async def send_message(self, session_id, *, parts, model, agent, system=None):
        user_text = parts[0].get("text", "")
        return [
            {
                "info": {"id": "u1", "role": "user"},
                "parts": [{"type": "text", "text": user_text}],
            },
            {
                "info": {"id": "a1", "role": "assistant"},
                "parts": [{"type": "text", "text": "assistant from list"}],
            },
        ]


@pytest.mark.asyncio
async def test_chat_api_accepts_opencode_list_response_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))

    app = create_app(Settings.from_env(), opencode_client=ListPayloadOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    resp = await client.post(
        "/api/chat",
        json={"message": "hello", "session_id": "s-list"},
    )
    assert resp.status == 200

    body = await resp.json()
    assert body["response"] == "assistant from list"
    assert body["usage"]["requests"] == 1
    assert body["usage"]["messages"] == 2
    assert body["usage"]["model"] == "unknown"
    assert body["usage"]["provider"] == "unknown"
    assert body["usage"]["input_tokens"] == 0
    assert body["usage"]["output_tokens"] == 0
    assert body["usage"]["cost"] == 0.0

    event_types = {event["type"] for event in body["runtime_events"]}
    assert "execution.started" in event_types
    assert "llm_thinking" in event_types
    assert "assistant_delta" in event_types
    assert "complete" in event_types
    assert "execution.completed" in event_types

    chatlog_resp = await client.get("/api/sessions/s-list/chatlog")
    assert chatlog_resp.status == 200
    chatlog = await chatlog_resp.json()
    assert chatlog["success"] is True
    assert chatlog["status"] == "success"
    assert chatlog["chatlog"]["entries"][-1]["status"] == "success"
    assert chatlog["chatlog"]["entries"][-1]["response"] == "assistant from list"
    assert any(e["type"] == "execution.completed" for e in chatlog["runtime_events"])

    usage_resp = await client.get("/api/usage?days=30")
    assert usage_resp.status == 200
    usage = await usage_resp.json()
    assert usage["global"]["total_requests"] >= 1
    assert usage["global"]["total_messages"] >= 2

    await client.close()


def test_usage_tracker_accepts_list_response_payload(tmp_path):
    tracker = UsageTracker(tmp_path / "usage.jsonl")

    rec = tracker.record_chat(
        session_id="s",
        request_id="r",
        model=None,
        provider=None,
        response_payload=[
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": "hello"}]},
            {
                "info": {"role": "assistant"},
                "parts": [{"type": "text", "text": "hi"}],
                "usage": {"input_tokens": 3, "output_tokens": 4, "cost": 0.01},
                "model": "m1",
                "provider": "p1",
            },
        ],
        input_text="hello",
        output_text="hi",
    )

    assert rec["model"] == "m1"
    assert rec["provider"] == "p1"
    assert rec["input_tokens"] == 3
    assert rec["output_tokens"] == 4
    assert rec["cost"] == 0.01
