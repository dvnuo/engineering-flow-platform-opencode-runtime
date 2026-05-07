from pathlib import Path

from efp_opencode_adapter.attachment_service import AttachmentService, build_attachment_context
from efp_opencode_adapter.settings import Settings


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        opencode_url="http://127.0.0.1:4096",
        adapter_state_dir=tmp_path / "state",
        workspace_dir=tmp_path / "workspace",
        skills_dir=tmp_path / "skills",
        tools_dir=tmp_path / "tools",
        opencode_data_dir=tmp_path / "opencode-state",
        opencode_config_path=tmp_path / "workspace/.opencode/opencode.json",
        opencode_version="1.14.39",
        opencode_server_username="opencode",
        opencode_server_password=None,
        ready_timeout_seconds=60,
    )


def test_attachment_lifecycle(tmp_path):
    settings = make_settings(tmp_path)
    svc = AttachmentService(settings)
    up = svc.upload("s1", "note.txt", b"revenue grows\n" * 400)
    fid = up["file_id"]
    base = settings.adapter_state_dir / "attachments" / "s1" / fid
    assert (base / "original").exists()
    assert (base / "metadata.json").exists()

    parsed = svc.parse(fid, "s1")
    assert parsed["success"] is True
    assert parsed["chunks"]
    assert (base / "parsed.json").exists()

    bad = svc.upload("s1", "x.pdf", b"%PDF-1.4\x00\x00")
    bad_parsed = svc.parse(bad["file_id"], "s1")
    assert bad_parsed == {"success": False, "error": "unsupported_file_type"}

    prev = svc.preview(fid, "s1")
    assert prev["success"] is True
    assert "revenue" in prev["preview"]

    p, _ = svc.download_path(fid, "s1")
    assert p.name == "original"

    ctx = build_attachment_context("s1", [{"file_id": fid}], settings=settings, max_chars=120)
    assert len(ctx) <= 120
    assert ctx.startswith("Attached files:")
    assert "## note.txt" in ctx
    assert "Attachment context truncated" in ctx

    deleted = svc.delete(fid, "s1")
    assert deleted["deleted"] is True
