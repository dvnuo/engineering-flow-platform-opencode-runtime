import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.app_keys import SESSION_STORE_KEY
from efp_opencode_adapter.opencode_client import OpenCodeClient
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.session_store import SessionRecord
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


class ContextOpenCodeClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.summarize_calls = []

    async def get_session_status(self, timeout_seconds=30):
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
            {
                "info": {
                    "id": "u-summary",
                    "role": "user",
                    "model": {"providerID": provider_id, "modelID": model_id},
                },
                "parts": [{"type": "text", "text": "Compacted conversation summary"}],
            },
            {
                "info": {
                    "id": "a-summary",
                    "role": "assistant",
                    "tokens": {"input": 200, "cache": {"read": 0, "write": 0}},
                },
                "parts": [{"type": "text", "text": "Ready to continue"}],
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

        assert compact_response.status == 200
        assert compacted["success"] is True
        assert compacted["before_message_count"] == 4
        assert compacted["after_message_count"] == 2
        assert compacted["after"]["used_tokens"] == 200
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
