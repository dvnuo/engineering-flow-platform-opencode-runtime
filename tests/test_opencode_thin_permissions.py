import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


class PermissionClient(FakeOpenCodeClient):
    async def get_session_status(self):
        return {"sessions": {sid: {"state": "idle"} for sid in self.sessions}}


async def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = PermissionClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    client = TestClient(TestServer(app))
    await client.start_server()
    created = await client.post("/api/opencode/conversations", json={"title": "Chat"})
    conversation = (await created.json())["conversation"]
    return client, fake, conversation


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"decision": "allow_once", "remember": True}, {"response": "once", "remember": False}),
        ({"decision": "allow_always", "remember": False}, {"response": "always", "remember": True}),
        ({"decision": "deny", "remember": True}, {"response": "reject", "remember": False}),
        ({"response": "once", "remember": False}, {"response": "once", "remember": False}),
        ({"response": "always", "remember": True}, {"response": "always", "remember": True}),
        ({"response": "reject"}, {"response": "reject"}),
    ],
)
async def test_thin_permission_mapping(tmp_path, monkeypatch, body, expected):
    client, fake, conversation = await _setup(tmp_path, monkeypatch)
    try:
        resp = await client.post(
            f"/api/opencode/conversations/{conversation['id']}/permissions/perm-1",
            json=body,
        )
        payload = await resp.json()

        assert resp.status == 200
        assert payload == {"ok": True, "source_of_truth": "opencode"}
        assert fake.permission_calls[-1] == {
            "session_id": "ses-1",
            "permission_id": "perm-1",
            "payload": expected,
        }
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_thin_permission_rejects_invalid_decision(tmp_path, monkeypatch):
    client, fake, conversation = await _setup(tmp_path, monkeypatch)
    try:
        resp = await client.post(
            f"/api/opencode/conversations/{conversation['id']}/permissions/perm-1",
            json={"decision": "allow"},
        )
        body = await resp.json()

        assert resp.status == 400
        assert body["error"] == "invalid_decision"
        assert fake.permission_calls == []
    finally:
        await client.close()
