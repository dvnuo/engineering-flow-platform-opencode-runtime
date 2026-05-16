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
        workspace_repos_dir=tmp_path / "workspace" / "repos",
        git_checkout_timeout_seconds=120,
        opencode_data_dir=tmp_path / "opencode-state",
        opencode_config_path=tmp_path / "workspace/.opencode/opencode.json",
        opencode_version="1.14.39",
        ready_timeout_seconds=60,
    )


def test_list_files_returns_portal_native_shape_for_directories(tmp_path):
    settings = make_settings(tmp_path)
    (settings.workspace_dir / ".opencode" / "agents").mkdir(parents=True)
    (settings.workspace_dir / ".opencode" / "opencode.json").write_text("{}")
    (settings.workspace_dir / "README.md").write_text("hello")
    svc = WorkspaceFileService(settings)

    ls = svc.list_files(".")
    assert ls["success"] is True
    assert ls["root_path"] == str(settings.workspace_dir.resolve())
    assert ls["path"] == str(settings.workspace_dir.resolve())

    items = {i["name"]: i for i in ls["items"]}
    opencode = items[".opencode"]
    assert opencode["is_dir"] is True
    assert opencode["is_file"] is False
    assert opencode["type"] == "directory"
    assert opencode["path"] == str((settings.workspace_dir / ".opencode").resolve())
    assert opencode["relative_path"] == ".opencode"

    readme = items["README.md"]
    assert readme["is_dir"] is False
    assert readme["is_file"] is True
    assert readme["type"] == "file"

    names = [i["name"] for i in ls["items"]]
    assert names.index(".opencode") < names.index("README.md")


def test_can_reopen_returned_absolute_directory_path(tmp_path):
    settings = make_settings(tmp_path)
    (settings.workspace_dir / ".opencode" / "agents").mkdir(parents=True)
    (settings.workspace_dir / ".opencode" / "opencode.json").write_text("{}")
    svc = WorkspaceFileService(settings)

    root = svc.list_files(".")
    op_path = next(i["path"] for i in root["items"] if i["name"] == ".opencode")

    nested = svc.list_files(op_path)
    assert nested["success"] is True
    assert nested["path"] == str((settings.workspace_dir / ".opencode").resolve())
    children = {i["name"] for i in nested["items"]}
    assert "agents" in children or "opencode.json" in children


def test_absolute_path_inside_workspace_allowed_and_outside_rejected(tmp_path):
    settings = make_settings(tmp_path)
    (settings.workspace_dir / ".opencode").mkdir(parents=True)
    svc = WorkspaceFileService(settings)

    inside = svc.resolve_workspace_path(str(settings.workspace_dir / ".opencode"))
    assert inside == (settings.workspace_dir / ".opencode").resolve()

    with pytest.raises(PermissionError):
        svc.resolve_workspace_path("/etc")
    with pytest.raises(PermissionError):
        svc.resolve_workspace_path("../secret")


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
    assert up["mode"] == "file_save"

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




def test_extract_zip_counts_only_files_not_directories(tmp_path):
    settings = make_settings(tmp_path)
    settings.workspace_dir.mkdir(parents=True)
    svc = WorkspaceFileService(settings)

    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr("x.txt", "x")
        zf.writestr("d/y.txt", "y")
        zf.writestr("empty/", "")

    body = svc.extract_zip_safely("uploads", "bundle.zip", payload.getvalue())
    assert body["mode"] == "zip_extract"
    assert body["items"] == ["uploads/d/y.txt", "uploads/x.txt"]
    assert "uploads/d" not in body["items"]
    assert "uploads/empty" not in body["items"]
    assert body["extracted_count"] == 2
    assert (settings.workspace_dir / "uploads" / "x.txt").exists()
    assert (settings.workspace_dir / "uploads" / "d" / "y.txt").exists()
    assert (settings.workspace_dir / "uploads" / "empty").exists()
    assert not (settings.workspace_dir / "uploads" / "bundle.zip").exists()

def test_upload_refuses_existing_symlink_escape(tmp_path):
    settings = make_settings(tmp_path)
    settings.workspace_dir.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    uploads = settings.workspace_dir / "uploads"
    uploads.mkdir()
    (uploads / "hello.txt").symlink_to(outside)

    svc = WorkspaceFileService(settings)
    with pytest.raises(PermissionError):
        svc.upload_file("uploads", "hello.txt", b"abc")

    assert outside.read_text() == "secret"


def test_directory_download_refuses_symlink_escape(tmp_path):
    settings = make_settings(tmp_path)
    settings.workspace_dir.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    leak = settings.workspace_dir / "leak"
    leak.mkdir()
    (leak / "secret.txt").symlink_to(outside)

    svc = WorkspaceFileService(settings)
    with pytest.raises(PermissionError):
        svc.prepare_download("leak")


def test_list_files_skips_symlinks(tmp_path):
    settings = make_settings(tmp_path)
    settings.workspace_dir.mkdir(parents=True)
    (settings.workspace_dir / "real.txt").write_text("ok")
    (settings.workspace_dir / "link.txt").symlink_to(settings.workspace_dir / "real.txt")

    svc = WorkspaceFileService(settings)
    names = {item["name"] for item in svc.list_files(".")["items"]}
    assert "real.txt" in names
    assert "link.txt" not in names


@pytest.mark.asyncio
async def test_download_route_accepts_repeated_paths_and_returns_zip(tmp_path):
    settings = make_settings(tmp_path)
    (settings.workspace_dir / "dir").mkdir(parents=True)
    (settings.workspace_dir / "a.txt").write_text("a")
    (settings.workspace_dir / "dir" / "b.txt").write_text("b")

    class FakeHealthy:
        async def health(self):
            return {"healthy": True, "version": "1.14.39"}

    client = TestClient(TestServer(create_app(settings, opencode_client=FakeHealthy())))
    await client.start_server()

    r = await client.get("/api/server-files/download?paths=a.txt&paths=dir")
    assert r.status == 200
    assert "zip" in (r.headers.get("Content-Type", "") + r.headers.get("Content-Disposition", "")).lower()
    data = await r.read()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
    assert "a.txt" in names
    assert "dir/b.txt" in names

    r2 = await client.get("/api/server-files/download", params={"path": "a.txt"})
    assert r2.status == 200
    assert await r2.read() == b"a"

    await client.close()


@pytest.mark.asyncio
async def test_delete_route_accepts_paths_list_and_recurses(tmp_path):
    settings = make_settings(tmp_path)
    (settings.workspace_dir / "dir").mkdir(parents=True)
    (settings.workspace_dir / "a.txt").write_text("a")
    (settings.workspace_dir / "dir" / "b.txt").write_text("b")

    class FakeHealthy:
        async def health(self):
            return {"healthy": True, "version": "1.14.39"}

    client = TestClient(TestServer(create_app(settings, opencode_client=FakeHealthy())))
    await client.start_server()

    r = await client.post("/api/server-files/delete", json={"paths": ["a.txt", "dir"]})
    assert r.status == 200
    assert (await r.json())["success"] is True
    assert not (settings.workspace_dir / "a.txt").exists()
    assert not (settings.workspace_dir / "dir").exists()

    r2 = await client.post("/api/server-files/delete", json={"paths": ["."]})
    assert r2.status == 403
    assert settings.workspace_dir.exists()

    r3 = await client.post("/api/server-files/delete", json={"paths": ["../secret"]})
    assert r3.status == 403

    r4 = await client.post("/api/server-files/delete", json={"paths": []})
    assert r4.status == 400

    await client.close()




@pytest.mark.asyncio
async def test_delete_route_deduplicates_duplicate_paths_without_half_success(tmp_path):
    settings = make_settings(tmp_path)
    settings.workspace_dir.mkdir(parents=True)
    (settings.workspace_dir / "a.txt").write_text("a")

    class FakeHealthy:
        async def health(self):
            return {"healthy": True, "version": "1.14.39"}

    client = TestClient(TestServer(create_app(settings, opencode_client=FakeHealthy())))
    await client.start_server()

    r = await client.post("/api/server-files/delete", json={"paths": ["a.txt", "a.txt"]})
    assert r.status == 200
    body = await r.json()
    assert body["success"] is True
    assert not (settings.workspace_dir / "a.txt").exists()
    assert len(body["deleted"]) == 1
    assert body["deleted"][0]["relative_path"] == "a.txt"

    await client.close()


@pytest.mark.asyncio
async def test_delete_route_collapses_parent_child_paths_without_half_success(tmp_path):
    settings = make_settings(tmp_path)
    (settings.workspace_dir / "dir" / "sub").mkdir(parents=True)
    (settings.workspace_dir / "dir" / "b.txt").write_text("b")
    (settings.workspace_dir / "dir" / "sub" / "c.txt").write_text("c")

    class FakeHealthy:
        async def health(self):
            return {"healthy": True, "version": "1.14.39"}

    client = TestClient(TestServer(create_app(settings, opencode_client=FakeHealthy())))
    await client.start_server()

    r = await client.post("/api/server-files/delete", json={"paths": ["dir", "dir/b.txt", "dir/sub/c.txt"]})
    assert r.status == 200
    body = await r.json()
    assert body["success"] is True
    assert not (settings.workspace_dir / "dir").exists()
    assert len(body["deleted"]) == 1
    assert body["deleted"][0]["relative_path"] == "dir"
    await client.close()


@pytest.mark.asyncio
async def test_delete_route_collapses_child_then_parent_without_half_success(tmp_path):
    settings = make_settings(tmp_path)
    (settings.workspace_dir / "dir").mkdir(parents=True)
    (settings.workspace_dir / "dir" / "b.txt").write_text("b")

    class FakeHealthy:
        async def health(self):
            return {"healthy": True, "version": "1.14.39"}

    client = TestClient(TestServer(create_app(settings, opencode_client=FakeHealthy())))
    await client.start_server()

    r = await client.post("/api/server-files/delete", json={"paths": ["dir/b.txt", "dir"]})
    assert r.status == 200
    body = await r.json()
    assert body["success"] is True
    assert not (settings.workspace_dir / "dir").exists()
    assert len(body["deleted"]) == 1
    assert body["deleted"][0]["relative_path"] == "dir"
    await client.close()

@pytest.mark.asyncio
async def test_delete_route_legacy_path_still_supported(tmp_path):
    settings = make_settings(tmp_path)
    (settings.workspace_dir / "src").mkdir(parents=True)
    (settings.workspace_dir / "src" / "x.txt").write_text("x")

    class FakeHealthy:
        async def health(self):
            return {"healthy": True, "version": "1.14.39"}

    client = TestClient(TestServer(create_app(settings, opencode_client=FakeHealthy())))
    await client.start_server()

    r1 = await client.post("/api/server-files/delete", json={"path": "."})
    assert r1.status == 403

    r2 = await client.post("/api/server-files/delete", json={"path": "src", "recursive": "true"})
    assert r2.status == 200
    assert not (settings.workspace_dir / "src").exists()

    await client.close()


@pytest.mark.asyncio
async def test_zip_upload_auto_extracts_by_filename_without_unzip_param(tmp_path):
    settings = make_settings(tmp_path)
    settings.workspace_dir.mkdir(parents=True)

    class FakeHealthy:
        async def health(self):
            return {"healthy": True, "version": "1.14.39"}

    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr("x.txt", "x")
        zf.writestr("d/y.txt", "y")

    client = TestClient(TestServer(create_app(settings, opencode_client=FakeHealthy())))
    await client.start_server()

    form = FormData()
    form.add_field("file", payload.getvalue(), filename="bundle.zip", content_type="application/zip")
    form.add_field("directory", "uploads")
    r = await client.post("/api/server-files/upload", data=form)
    assert r.status == 200
    body = await r.json()
    assert body["success"] is True
    assert body["mode"] == "zip_extract"
    assert body["uploaded_filename"] == "bundle.zip"
    assert body["extracted_count"] == 2
    assert body["target_path"] == str((settings.workspace_dir / "uploads").resolve())
    assert (settings.workspace_dir / "uploads" / "x.txt").exists()
    assert (settings.workspace_dir / "uploads" / "d" / "y.txt").exists()
    assert not (settings.workspace_dir / "uploads" / "bundle.zip").exists()

    await client.close()


@pytest.mark.asyncio
async def test_non_zip_upload_still_file_save(tmp_path):
    settings = make_settings(tmp_path)
    settings.workspace_dir.mkdir(parents=True)

    class FakeHealthy:
        async def health(self):
            return {"healthy": True, "version": "1.14.39"}

    client = TestClient(TestServer(create_app(settings, opencode_client=FakeHealthy())))
    await client.start_server()

    form = FormData()
    form.add_field("file", b"hello", filename="hello.txt", content_type="text/plain")
    form.add_field("directory", "uploads")
    r = await client.post("/api/server-files/upload", data=form)
    assert r.status == 200
    body = await r.json()
    assert body["mode"] == "file_save"
    assert body["uploaded_filename"] == "hello.txt"
    assert body["path"] == "uploads/hello.txt"
    assert body["size"] == 5
    assert body["content_type"] == "text/plain"

    await client.close()


@pytest.mark.asyncio
async def test_invalid_zip_filename_returns_400_without_partial_files(tmp_path):
    settings = make_settings(tmp_path)
    settings.workspace_dir.mkdir(parents=True)

    class FakeHealthy:
        async def health(self):
            return {"healthy": True, "version": "1.14.39"}

    client = TestClient(TestServer(create_app(settings, opencode_client=FakeHealthy())))
    await client.start_server()

    form = FormData()
    form.add_field("file", b"not-a-zip", filename="bad.zip", content_type="application/zip")
    form.add_field("directory", "uploads")
    r = await client.post("/api/server-files/upload", data=form)
    assert r.status == 400
    body = await r.json()
    assert body["error"] == "invalid_zip_file"
    assert not (settings.workspace_dir / "uploads" / "bad.zip").exists()

    await client.close()


@pytest.mark.asyncio
async def test_legacy_alias_routes(tmp_path):
    settings = make_settings(tmp_path)
    settings.workspace_dir.mkdir(parents=True)
    (settings.workspace_dir / "README.md").write_text("hello")

    class FakeHealthy:
        async def health(self):
            return {"healthy": True, "version": "1.14.39"}

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

    await client.close()
