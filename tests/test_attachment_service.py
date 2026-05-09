from pathlib import Path

from efp_opencode_adapter.attachment_service import AttachmentService, build_attachment_context, build_opencode_attachment_parts, normalize_attachment_refs
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


def test_normalize_attachment_refs():
    refs = normalize_attachment_refs(["a1", {"file_id": "a2", "name": "n"}, {"id": "a3"}, 123, {}, ""])
    assert [r["file_id"] for r in refs] == ["a1", "a2", "a3"]


def test_build_opencode_attachment_parts_for_text_and_missing(tmp_path):
    settings = make_settings(tmp_path)
    svc = AttachmentService(settings)
    up = svc.upload("s1", "notes.txt", b"hello file", "text/plain")
    parts, debug = build_opencode_attachment_parts(svc, "s1", [up["file_id"], "does-not-exist"])
    text_parts = [p for p in parts if p.get("type") == "text"]
    assert any("hello file" in p.get("text", "") and "notes.txt" in p.get("text", "") for p in text_parts)
    assert any("could not be loaded" in p.get("text", "") for p in text_parts)
    assert any(d.get("status") == "error" for d in debug)


def test_text_truncation_still_processes_image_and_pdf(tmp_path):
    settings = make_settings(tmp_path)
    svc = AttachmentService(settings)
    big = svc.upload("s1", "big.txt", b"a" * 500, "text/plain")
    img = svc.upload("s1", "cat.png", b"\x89PNG\r\n\x1a\nabc", "image/png")
    pdf = svc.upload("s1", "doc.pdf", b"%PDF-1.4\nabc", "application/pdf")
    parts, debug = build_opencode_attachment_parts(svc, "s1", [big["file_id"], img["file_id"], pdf["file_id"]], max_text_chars=80)
    assert any(p.get("type") == "text" and "[Attachment context truncated]" in p.get("text", "") for p in parts)
    assert any(p.get("type") == "file" and p.get("mime") == "image/png" for p in parts)
    assert any(p.get("type") == "file" and p.get("mime") == "application/pdf" for p in parts)
    assert any(d.get("file_id") == big["file_id"] and d.get("truncated") is True for d in debug)
    assert any(d.get("file_id") == img["file_id"] and d.get("inlined") is True for d in debug)


def test_text_budget_exhausted_next_text_not_unsupported(tmp_path):
    settings = make_settings(tmp_path)
    svc = AttachmentService(settings)
    big1 = svc.upload("s1", "big1.txt", b"a" * 500, "text/plain")
    big2 = svc.upload("s1", "big2.txt", b"b" * 300, "text/plain")
    parts, debug = build_opencode_attachment_parts(svc, "s1", [big1["file_id"], big2["file_id"]], max_text_chars=80)
    assert not any("runtime cannot parse or inline this file type yet" in p.get("text", "") and "big2.txt" in p.get("text", "") for p in parts if p.get("type") == "text")
    assert any(d.get("file_id") == big2["file_id"] and d.get("action") == "text_context" and d.get("truncated") is True for d in debug)


def test_invalid_attachment_refs_and_non_list_and_service_none(tmp_path):
    settings = make_settings(tmp_path)
    svc = AttachmentService(settings)
    valid = svc.upload("s1", "ok.txt", b"ok", "text/plain")
    parts, debug = build_opencode_attachment_parts(svc, "s1", [123, {}, {"name": "x"}, "", {"id": valid["file_id"]}])
    assert any("ok.txt" in p.get("text", "") for p in parts if p.get("type") == "text")
    assert any(d.get("status") in {"skipped", "error"} and d.get("error") == "invalid_attachment_ref" for d in debug)
    assert not any("Attachments could not be processed" in p.get("text", "") for p in parts)

    parts2, debug2 = build_opencode_attachment_parts(svc, "s1", {"file_id": "x"})
    assert any("attachments must be a list" in p.get("text", "") for p in parts2)
    assert any(d.get("error") == "attachments_not_list" for d in debug2)

    parts3, debug3 = build_opencode_attachment_parts(None, "s1", ["x"])
    assert any("attachment service is unavailable" in p.get("text", "") for p in parts3)
    assert any(d.get("error") == "attachment_service_unavailable" for d in debug3)
