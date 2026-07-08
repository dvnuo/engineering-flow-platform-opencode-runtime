"""Workspace file management parity for the opencode adapter.

Mirrors the native runtime: move/mkdir/new-file (B1/B2), read_file cap +
binary handling (A3), and the download temp archive being streamed then
cleaned up (A1, fixes the previous leak).
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.app_keys import SETTINGS_KEY
from efp_opencode_adapter.file_routes import register_file_routes
from efp_opencode_adapter.file_service import WorkspaceFileService
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
        efp_config_path=tmp_path / "workspace" / ".efp" / "config.yaml",
        mobile_state_dir=tmp_path / "workspace" / ".efp" / "mobile-auto" / "runs",
        mobile_artifacts_dir=tmp_path / "workspace" / ".efp" / "mobile-auto" / "artifacts",
        browserstack_local_binary_path=Path("/usr/local/bin/BrowserStackLocal"),
        opencode_version="1.14.39",
        ready_timeout_seconds=60,
    )


def _svc(tmp_path: Path) -> WorkspaceFileService:
    settings = make_settings(tmp_path)
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    return WorkspaceFileService(settings)


# --------------------------------------------------------------------------- #
# Service-level
# --------------------------------------------------------------------------- #

def test_move_renames_file(tmp_path):
    svc = _svc(tmp_path)
    (svc.root / "a.txt").write_text("hi", encoding="utf-8")
    result = svc.move_path("a.txt", "b.txt")
    assert result["relative_path"] == "b.txt"
    assert not (svc.root / "a.txt").exists()
    assert (svc.root / "b.txt").read_text(encoding="utf-8") == "hi"


def test_move_rejects_overwrite(tmp_path):
    svc = _svc(tmp_path)
    (svc.root / "a.txt").write_text("a", encoding="utf-8")
    (svc.root / "b.txt").write_text("b", encoding="utf-8")
    with pytest.raises(ValueError):
        svc.move_path("a.txt", "b.txt")


def test_move_rejects_escape(tmp_path):
    svc = _svc(tmp_path)
    (svc.root / "a.txt").write_text("a", encoding="utf-8")
    with pytest.raises(PermissionError):
        svc.move_path("a.txt", "../escape.txt")


def test_mkdir_creates_nested(tmp_path):
    svc = _svc(tmp_path)
    result = svc.make_directory("x/y/z")
    assert result["is_dir"] is True
    assert (svc.root / "x" / "y" / "z").is_dir()


def test_mkdir_rejects_existing(tmp_path):
    svc = _svc(tmp_path)
    (svc.root / "d").mkdir()
    with pytest.raises(ValueError):
        svc.make_directory("d")


def test_new_file_creates_empty_with_parents(tmp_path):
    svc = _svc(tmp_path)
    result = svc.create_file("notes/todo.md")
    assert result["size"] == 0
    assert (svc.root / "notes" / "todo.md").read_bytes() == b""


def test_read_file_truncates_over_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_MAX_READ_FILE_BYTES", "10")
    svc = _svc(tmp_path)
    (svc.root / "big.txt").write_text("x" * 25, encoding="utf-8")
    result = svc.read_file("big.txt")
    assert result["truncated"] is True
    assert result["returned_bytes"] == 10
    assert result["size"] == 25
    assert result["content"] == "x" * 10


def test_read_file_flags_binary(tmp_path):
    svc = _svc(tmp_path)
    (svc.root / "blob.bin").write_bytes(b"\x00\x01\x02")
    result = svc.read_file("blob.bin")
    assert result["is_binary"] is True
    assert result["content"] == ""


def test_prepare_download_single_file_not_temp(tmp_path):
    svc = _svc(tmp_path)
    (svc.root / "a.txt").write_text("hi", encoding="utf-8")
    path, name, ctype, is_temp = svc.prepare_download("a.txt")
    assert is_temp is False
    assert name == "a.txt"


def test_prepare_download_directory_is_temp_zip(tmp_path):
    svc = _svc(tmp_path)
    (svc.root / "d").mkdir()
    (svc.root / "d" / "one.txt").write_text("1", encoding="utf-8")
    path, name, ctype, is_temp = svc.prepare_download("d")
    try:
        assert is_temp is True
        assert ctype == "application/zip"
        with zipfile.ZipFile(path) as zf:
            assert any(n.endswith("one.txt") for n in zf.namelist())
    finally:
        Path(path).unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Route-level
# --------------------------------------------------------------------------- #

async def _client(tmp_path: Path) -> TestClient:
    settings = make_settings(tmp_path)
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    app = web.Application()
    app[SETTINGS_KEY] = settings
    register_file_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_mkdir_move_new_file_routes(tmp_path):
    client = await _client(tmp_path)
    try:
        r = await client.post("/api/server-files/mkdir", json={"path": "docs"})
        assert r.status == 200 and (await r.json())["is_dir"] is True

        r = await client.post("/api/server-files/new-file", json={"path": "docs/readme.md"})
        assert r.status == 200 and (await r.json())["size"] == 0

        r = await client.post(
            "/api/server-files/move",
            json={"source": "docs/readme.md", "destination": "docs/guide.md"},
        )
        assert r.status == 200 and (await r.json())["relative_path"] == "docs/guide.md"

        r = await client.post("/api/server-files/mkdir", json={})
        assert r.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_directory_download_streams_and_cleans_temp(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    (settings.workspace_dir / "d").mkdir()
    (settings.workspace_dir / "d" / "a.txt").write_text("alpha", encoding="utf-8")

    import efp_opencode_adapter.file_service as fs
    created: list[Path] = []
    real = fs.tempfile.NamedTemporaryFile

    def _tracking(*args, **kwargs):
        handle = real(*args, **kwargs)
        created.append(Path(handle.name))
        return handle

    monkeypatch.setattr(fs.tempfile, "NamedTemporaryFile", _tracking)

    app = web.Application()
    app[SETTINGS_KEY] = settings
    register_file_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/api/server-files/download", params={"path": "d"})
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "application/zip"
        body = await resp.read()
    finally:
        await client.close()

    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        assert any(n.endswith("a.txt") for n in zf.namelist())
    assert created, "expected a temp archive"
    assert all(not p.exists() for p in created), "temp archive was not cleaned up"
