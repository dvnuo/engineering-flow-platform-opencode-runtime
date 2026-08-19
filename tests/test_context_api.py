import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.app_keys import SESSION_STORE_KEY
from efp_opencode_adapter.context_api import _active_context_messages
from efp_opencode_adapter.opencode_client import OpenCodeClient
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.session_store import SessionRecord
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


class ContextOpenCodeClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.summarize_calls = []
        self.status_error = None

    async def get_session_status(self, timeout_seconds=30):
        if self.status_error is not None:
            raise self.status_error
        return {}

    async def list_tools(self, provider_id, model_id, timeout_seconds=30):
        return [
            {
                "id": "read",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        ]

    async def list_providers(self, timeout_seconds=30):
        return {
            "all": [
                {
                    "id": "github-copilot",
                    "models": {"gpt-5.4": {"limit": {"context": 2_000}}},
                }
            ]
        }

    async def summarize_session(
        self,
        session_id,
        *,
        provider_id,
        model_id,
        auto=False,
        timeout_seconds=180,
    ):
        self.summarize_calls.append(
            {
                "session_id": session_id,
                "provider_id": provider_id,
                "model_id": model_id,
                "auto": auto,
            }
        )
        self.messages[session_id] = [
            *self.messages[session_id],
            {
                "info": {
                    "id": "u-compact",
                    "role": "user",
                    "model": {"providerID": provider_id, "modelID": model_id},
                },
                "parts": [{"type": "compaction", "auto": False}],
            },
            {
                "info": {
                    "id": "a-summary",
                    "role": "assistant",
                    "parentID": "u-compact",
                    "summary": True,
                    "finish": "stop",
                    "mode": "compaction",
                    "modelID": model_id,
                    "providerID": provider_id,
                    "tokens": {"input": 800, "cache": {"read": 0, "write": 0}},
                },
                "parts": [{"type": "text", "text": "Compacted conversation summary"}],
            },
        ]
        return True


@pytest.mark.asyncio
async def test_opencode_client_uses_pinned_tool_and_summarize_routes(monkeypatch):
    client = OpenCodeClient(Settings.from_env())
    calls = []

    async def request_json(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path.startswith("/experimental/tool?"):
            return [{"id": "read"}]
        return True

    monkeypatch.setattr(client, "_request_json", request_json)

    tools = await client.list_tools("github-copilot", "gpt-5.4")
    await client.summarize_session(
        "oc-1",
        provider_id="github-copilot",
        model_id="gpt-5.4",
    )

    assert tools == [{"id": "read"}]
    assert calls[0][0:2] == (
        "GET",
        "/experimental/tool?provider=github-copilot&model=gpt-5.4",
    )
    assert calls[1][0:2] == ("POST", "/session/oc-1/summarize")
    assert calls[1][2]["json"] == {
        "providerID": "github-copilot",
        "modelID": "gpt-5.4",
        "auto": False,
    }


@pytest.mark.asyncio
async def test_context_usage_and_manual_compact_share_portal_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = ContextOpenCodeClient()
    fake.sessions["oc-context"] = {"id": "oc-context", "title": "Context"}
    fake.messages["oc-context"] = [
        {
            "info": {
                "id": "u-1",
                "role": "user",
                "model": {"providerID": "github-copilot", "modelID": "gpt-5.4"},
            },
            "parts": [{"type": "text", "text": "Inspect the repository"}],
        },
        {
            "info": {"id": "a-1", "role": "assistant"},
            "parts": [
                {"type": "tool", "tool": "read", "state": {"output": "file contents"}},
                {"type": "text", "text": "I inspected it"},
            ],
        },
        {
            "info": {
                "id": "u-2",
                "role": "user",
                "model": {"providerID": "github-copilot", "modelID": "gpt-5.4"},
            },
            "parts": [{"type": "text", "text": "Continue"}],
        },
        {
            "info": {
                "id": "a-2",
                "role": "assistant",
                "tokens": {"input": 800, "cache": {"read": 0, "write": 0}},
            },
            "parts": [{"type": "text", "text": "Done"}],
        },
    ]
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            "portal-context",
            "oc-context",
            "Context",
            None,
            "github-copilot/gpt-5.4",
            "a",
            "b",
            "",
            4,
        )
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get("/api/sessions/portal-context/context-usage")
        payload = await response.json()

        assert response.status == 200
        assert payload["success"] is True
        assert payload["engine"] == "opencode"
        assert payload["used_tokens"] == 800
        assert payload["usage_percent"] == 40.0
        assert {item["id"] for item in payload["categories"]} == {
            "instructions",
            "tool_definitions",
            "conversation",
            "tool_activity",
        }
        assert payload["compact"]["eligible"] is True

        compact_response = await client.post("/api/sessions/portal-context/compact", json={})
        compacted = await compact_response.json()
        refreshed_response = await client.get("/api/sessions/portal-context/context-usage")
        refreshed = await refreshed_response.json()

        assert compact_response.status == 200
        assert compacted["success"] is True
        assert compacted["before_message_count"] == 4
        assert compacted["after_message_count"] == 1
        assert compacted["persisted_before_message_count"] == 4
        assert compacted["persisted_after_message_count"] == 6
        assert compacted["after"]["scope"] == "current_estimate"
        assert compacted["after"]["used_tokens"] < compacted["before"]["used_tokens"]
        assert compacted["after"]["compact"]["eligible"] is False
        assert refreshed_response.status == 200
        assert refreshed["used_tokens"] == compacted["after"]["used_tokens"]
        assert refreshed["scope"] == "current_estimate"
        assert fake.summarize_calls == [
            {
                "session_id": "oc-context",
                "provider_id": "github-copilot",
                "model_id": "gpt-5.4",
                "auto": False,
            }
        ]
    finally:
        await client.close()


def test_active_context_messages_honors_compaction_tail_boundary():
    messages = [
        {"info": {"id": "u-1", "role": "user"}, "parts": [{"type": "text", "text": "old"}]},
        {"info": {"id": "a-1", "role": "assistant"}, "parts": [{"type": "text", "text": "old"}]},
        {"info": {"id": "u-2", "role": "user"}, "parts": [{"type": "text", "text": "recent"}]},
        {"info": {"id": "a-2", "role": "assistant"}, "parts": [{"type": "text", "text": "recent"}]},
        {
            "info": {"id": "u-compact", "role": "user"},
            "parts": [{"type": "compaction", "tail_start_id": "u-2"}],
        },
        {
            "info": {
                "id": "a-summary",
                "parentID": "u-compact",
                "role": "assistant",
                "summary": True,
                "finish": "stop",
            },
            "parts": [{"type": "text", "text": "summary"}],
        },
    ]

    active = _active_context_messages(messages)

    assert [message["info"]["id"] for message in active] == [
        "u-2",
        "a-2",
        "u-compact",
        "a-summary",
    ]


@pytest.mark.asyncio
async def test_status_failure_disables_get_and_blocks_manual_compact(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = ContextOpenCodeClient()
    fake.status_error = RuntimeError("status unavailable")
    fake.sessions["oc-context"] = {"id": "oc-context", "title": "Context"}
    fake.messages["oc-context"] = [
        {
            "info": {
                "id": "u-1",
                "role": "user",
                "model": {"providerID": "github-copilot", "modelID": "gpt-5.4"},
            },
            "parts": [{"type": "text", "text": "Question"}],
        },
        {
            "info": {"id": "a-1", "role": "assistant"},
            "parts": [{"type": "text", "text": "Answer"}],
        },
    ]
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[SESSION_STORE_KEY].upsert(
        SessionRecord(
            "portal-context",
            "oc-context",
            "Context",
            None,
            "github-copilot/gpt-5.4",
            "a",
            "b",
            "",
            2,
        )
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        usage_response = await client.get("/api/sessions/portal-context/context-usage")
        usage = await usage_response.json()
        compact_response = await client.post("/api/sessions/portal-context/compact", json={})
        compact = await compact_response.json()

        assert usage_response.status == 200
        assert usage["compact"]["eligible"] is False
        assert usage["compact"]["runtime_state"] == "unavailable"
        assert "verify" in usage["compact"]["reason"].lower()
        assert compact_response.status == 503
        assert compact["error"] == "session_status_unavailable"
        assert fake.summarize_calls == []
    finally:
        await client.close()
