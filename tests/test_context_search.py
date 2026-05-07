import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings


def make_settings(tmp_path):
    return Settings(
        opencode_url="http://127.0.0.1:4096",
        adapter_state_dir=tmp_path / "state",
        workspace_dir=tmp_path / "workspace",
        skills_dir=tmp_path / "skills",
        tools_dir=tmp_path / "tools",
        opencode_data_dir=tmp_path / "opencode-state",
        opencode_config_path=tmp_path / "workspace/.opencode/opencode.json",
        opencode_version="1.14.39",
        ready_timeout_seconds=60,
    )


class FakeHealthyClient:
    async def health(self):
        return {"healthy": True, "version": "1.14.39"}


@pytest.mark.asyncio
async def test_context_and_search_routes(tmp_path):
    settings = make_settings(tmp_path)
    settings.workspace_dir.mkdir(parents=True)
    client = TestClient(TestServer(create_app(settings, opencode_client=FakeHealthyClient())))
    await client.start_server()

    form = FormData()
    form.add_field("file", b"revenue and margin", filename="note.txt", content_type="text/plain")
    up = await client.post("/api/files/upload?session_id=s1", data=form)
    assert up.status == 200
    up_json = await up.json()
    assert up_json["size"] == len(b"revenue and margin")
    fid = up_json["file_id"]
    original = settings.adapter_state_dir / "attachments" / "s1" / fid / "original"
    assert original.read_bytes() == b"revenue and margin"

    parsed = await client.post("/api/files/parse", json={"session_id": "s1", "file_id": fid})
    assert parsed.status == 200
    parsed_json = await parsed.json()
    assert parsed_json["success"] is True
    assert parsed_json["text"] == "revenue and margin"
    assert parsed_json["chunks"]
    assert parsed_json["chunks"][0]["content"] == "revenue and margin"

    ctx = await client.get("/api/context/files", params={"session_id": "s1"})
    assert ctx.status == 200
    assert (await ctx.json())["files"]

    search = await client.get("/api/chunks/search", params={"session_id": "s1", "q": "revenue", "top_k": "5"})
    assert search.status == 200
    data = await search.json()
    assert data["total"] >= 1

    search2 = await client.get("/api/chunks/search", params={"session_id": "s1", "query": "revenue"})
    assert search2.status == 200

    listed = await client.get("/api/files/list", params={"session_id": "s1"})
    assert listed.status == 200

    prev = await client.get(f"/api/files/{fid}/preview", params={"session_id": "s1"})
    assert prev.status == 200

    deleted = await client.delete(f"/api/files/{fid}", params={"session_id": "s1"})
    assert deleted.status == 200
    await client.close()
