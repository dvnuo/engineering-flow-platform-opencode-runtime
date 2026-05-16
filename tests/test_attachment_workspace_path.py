from pathlib import Path

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.attachment_service import AttachmentService
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings


def make_settings(tmp_path: Path, monkeypatch) -> Settings:
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENCODE_CONFIG", str(tmp_path / "workspace/.opencode/opencode.json"))
    return Settings.from_env()


def test_csv_upload_returns_workspace_path_and_materializes_file(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    svc = AttachmentService(settings)
    content = b"summary,description\nOne,Two\n"

    uploaded = svc.upload("../session", "../issues.csv", content, "text/csv")
    workspace_path = Path(uploaded["workspace_path"])
    uploads_root = (settings.workspace_dir / "uploads").resolve()

    assert uploaded["name"] == "issues.csv"
    assert workspace_path.exists()
    assert workspace_path.read_bytes() == content
    assert workspace_path.resolve().is_relative_to(uploads_root)
    assert workspace_path.name == "issues.csv"

    metadata = svc.get_metadata(uploaded["file_id"], uploaded["session_id"])
    assert metadata["workspace_path"] == str(workspace_path)

    listed = svc.list_files(uploaded["session_id"])["files"][0]
    assert listed["workspace_path"] == str(workspace_path)

    context_file = svc.context_files(uploaded["session_id"])["files"][0]
    assert context_file["workspace_path"] == str(workspace_path)


class FakeHealthyClient:
    async def health(self):
        return {"healthy": True, "version": "1.14.39"}


@pytest.mark.asyncio
async def test_upload_api_returns_workspace_path_and_materializes_csv(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    client = TestClient(TestServer(create_app(settings, opencode_client=FakeHealthyClient())))
    await client.start_server()
    content = b"summary,description\nOne,Two\n"

    try:
        form = FormData()
        form.add_field("file", content, filename="issues.csv", content_type="text/csv")
        response = await client.post("/api/files/upload?session_id=csv/session", data=form)

        assert response.status == 200
        payload = await response.json()
        workspace_path = Path(payload["workspace_path"])
        uploads_root = (settings.workspace_dir / "uploads").resolve()

        assert payload["success"] is True
        assert payload["session_id"] == "csv-session"
        assert payload["name"] == "issues.csv"
        assert workspace_path.exists()
        assert workspace_path.read_bytes() == content
        assert workspace_path.resolve().is_relative_to(uploads_root)

        metadata = AttachmentService(settings).get_metadata(payload["file_id"], payload["session_id"])
        assert metadata["workspace_path"] == str(workspace_path)
    finally:
        await client.close()
