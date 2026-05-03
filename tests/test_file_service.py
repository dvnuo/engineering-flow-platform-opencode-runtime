import io
import zipfile
from pathlib import Path

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.file_service import WorkspaceFileService
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        opencode_url="http://127.0.0.1:4096",
        adapter_state_dir=tmp_path / "state",
        workspace_dir=tmp_path / "workspace",
        skills_dir=tmp_path / "skills",
        tools_dir=tmp_path / "tools",
        opencode_config_path=tmp_path / "workspace/.opencode/opencode.json",
        opencode_version="1.14.29",
        opencode_server_username="opencode",
        opencode_server_password=None,
        ready_timeout_seconds=60,
    )


def test_workspace_service_core(tmp_path):
    settings = make_settings(tmp_path)
    settings.workspace_dir.mkdir(parents=True)
    (settings.workspace_dir / "README.md").write_text("hello")
    (settings.workspace_dir / "src").mkdir()
    (settings.workspace_dir / "src/main.py").write_text("print('x')")
    svc = WorkspaceFileService(settings)

    with pytest.raises(PermissionError):
        svc.resolve_workspace_path("../secret")
    with pytest.raises(PermissionError):
        svc.resolve_workspace_path("/etc/passwd")

    ls = svc.list_files(".")
    assert ls["success"] is True
    assert {i["name"] for i in ls["items"]} >= {"README.md", "src"}

    rd = svc.read_file("README.md")
    assert rd["language"] == "markdown"
    assert "hello" in rd["content"]

    up = svc.upload_file("uploads", "hello.txt", b"abc")
    assert up["path"] == "uploads/hello.txt"

    safe = io.BytesIO()
    with zipfile.ZipFile(safe, "w") as zf:
        zf.writestr("a.txt", "1")
        zf.writestr("b/c.txt", "2")
    out = svc.extract_zip_safely("unzipped", "safe.zip", safe.getvalue())
    assert "unzipped/a.txt" in out["items"]

    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../evil.txt", "no")
    with pytest.raises(PermissionError):
        svc.extract_zip_safely(".", "evil.zip", evil.getvalue())
    assert not (settings.workspace_dir / "evil.txt").exists()

    assert svc.delete_path("uploads/hello.txt")["deleted"] is True
    with pytest.raises(OSError):
        svc.delete_path("src")
    assert svc.delete_path("src", recursive=True)["deleted"] is True


@pytest.mark.asyncio
async def test_legacy_alias_routes(tmp_path):
    settings = make_settings(tmp_path)
    settings.workspace_dir.mkdir(parents=True)
    (settings.workspace_dir / "README.md").write_text("hello")

    class FakeHealthy:
        async def health(self):
            return {"healthy": True, "version": "1.14.29"}

    client = TestClient(TestServer(create_app(settings, opencode_client=FakeHealthy())))
    await client.start_server()
    r1 = await client.get("/api/files", params={"path": "."})
    assert r1.status == 200
    p1 = await r1.json()
    assert p1["success"] is True

    r2 = await client.get("/api/files/read", params={"path": "README.md"})
    assert r2.status == 200
    p2 = await r2.json()
    assert "hello" in p2["content"]

    form = FormData()
    form.add_field("file", b"abc", filename="hello.txt")
    form.add_field("directory", "uploads")
    r3 = await client.post("/api/server-files/upload", data=form)
    assert r3.status == 200

    await client.close()
