"""Attachments the chat model cannot read are handed to the agent, never guessed.

- The chat provider decides whether attached images are sent to OpenCode as
  file parts (AI Platform) or described to the agent (GitHub Copilot, unless
  EFP_COPILOT_VISION_VIA_CHAT says vision is back).
- The office formats the adapter does not parse (doc, dot, xls) always go to
  the handoff block, and this runtime has no browser tool, so the block says
  the files cannot be analyzed here.
- Boot records only whether the provider sees images; nothing about
  inspect-image is projected any more.
"""
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter import vision
from efp_opencode_adapter.app_keys import ATTACHMENT_SERVICE_KEY, BOOT_PROJECTION_KEY
from efp_opencode_adapter.attachment_handoff import (
    ATTACHED_FILES_HEADER,
    ATTACHED_FILES_RULE,
    HANDOFF_HEADER,
    HANDOFF_NO_BROWSER_TOOL,
    build_attachment_handoff,
    is_forwarded_mime,
)
from efp_opencode_adapter.attachment_service import AttachmentService, build_opencode_attachment_parts
from efp_opencode_adapter.portal_runtime_context_bootstrap import apply_boot_projection
from efp_opencode_adapter.profile_store import ProfileOverlayStore
from efp_opencode_adapter.runtime_profile_projection import project_canonical_for_runtime
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient

PNG_BYTES = b"\x89PNG\r\n\x1a\nabc"
OLE_BYTES = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 8


def _llm(provider="github_copilot"):
    return {
        "provider": provider,
        "model": "gpt-5.6-terra",
        "api_key": "gho_TEST",
        "ai_platform": {
            "chat": {"host": "https://chat.int", "uri": "/v1/api/v1/chat/completions"},
            "ib2b": {"host": "https://ib2b.int", "uri": "/dsp/token"},
            "auth": {"username": "u", "password": "pw", "usercase": "uc", "trust_token_header": "X-Trust", "tracking_prefix": "EFP"},
        },
    }


def _payload(config: dict, profile_id="rp-files", revision=5) -> dict:
    return {"runtime_profile_id": profile_id, "name": "profile", "revision": revision, "config": config}


# --- which provider sees images -------------------------------------------------


def test_chat_provider_accepts_images(monkeypatch):
    monkeypatch.delenv(vision.COPILOT_VISION_VIA_CHAT_ENV, raising=False)
    assert vision.chat_provider_accepts_images({"provider": "ai-platform"})
    assert vision.chat_provider_accepts_images({"provider": "ai_platform"})
    assert not vision.chat_provider_accepts_images({"provider": "github-copilot"})
    assert not vision.chat_provider_accepts_images({})
    assert not vision.chat_provider_accepts_images(None)
    monkeypatch.setenv(vision.COPILOT_VISION_VIA_CHAT_ENV, "1")
    assert vision.chat_provider_accepts_images({"provider": "github-copilot"})
    assert not vision.chat_provider_accepts_images({"provider": "github-copilot"}, environ={vision.COPILOT_VISION_VIA_CHAT_ENV: "off"})


def test_forwarded_mime_types():
    assert is_forwarded_mime("application/msword")
    assert is_forwarded_mime("application/vnd.ms-excel; charset=binary")
    assert not is_forwarded_mime("application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert not is_forwarded_mime("image/png")
    assert not is_forwarded_mime(None)


# --- the handoff block --------------------------------------------------------------


def test_build_attachment_handoff_text():
    files = [
        {"path": "/workspace/uploads/s1/f1/shot.png", "name": "shot.png", "content_type": "image/png", "size_bytes": 12, "status": "unreadable"},
        {"path": "/workspace/uploads/s1/f2/plan.xls", "name": "Q3 plan.xls", "content_type": "application/vnd.ms-excel", "size_bytes": 2048, "status": "unreadable"},
        {"path": "/workspace/uploads/s1/f3/notes.txt", "name": "notes.txt", "content_type": "text/plain", "size_bytes": 10, "status": "excerpt"},
        {"path": "", "name": "ignored.png", "content_type": "image/png", "size_bytes": 1, "status": "unreadable"},
    ]
    text = build_attachment_handoff(files)
    assert text.startswith(ATTACHED_FILES_HEADER)
    assert "- /workspace/uploads/s1/f1/shot.png (image/png, 12 bytes; not readable by the chat model)" in text
    assert (
        '- /workspace/uploads/s1/f2/plan.xls (application/vnd.ms-excel, 2048 bytes, attached as "Q3 plan.xls"; '
        "not readable by the chat model)"
    ) in text
    assert "- /workspace/uploads/s1/f3/notes.txt (text/plain, 10 bytes; an excerpt of it is in this prompt)" in text
    assert "ignored.png" not in text
    assert text.index(ATTACHED_FILES_RULE) > text.index("notes.txt")
    assert text.index(HANDOFF_HEADER) > text.index(ATTACHED_FILES_RULE)
    assert text.endswith(HANDOFF_NO_BROWSER_TOOL)
    assert "inspect-image" not in text and "never guess" in text
    assert build_attachment_handoff([]) == ""

    # Nothing the model cannot read: the list and the rule, no analysis wording.
    readable = build_attachment_handoff([files[2]])
    assert readable.startswith(ATTACHED_FILES_HEADER) and readable.endswith(ATTACHED_FILES_RULE)
    assert HANDOFF_HEADER not in readable and HANDOFF_NO_BROWSER_TOOL not in readable


def test_attachment_parts_hand_unreadable_files_to_the_agent():
    settings = Settings.from_env()
    svc = AttachmentService(settings)
    image = svc.upload("s1", "shot.png", PNG_BYTES, "image/png")
    sheet = svc.upload("s1", "plan.xls", OLE_BYTES, "application/vnd.ms-excel")
    note = svc.upload("s1", "notes.txt", b"hello file", "text/plain")

    parts, debug = build_opencode_attachment_parts(
        svc, "s1", [image["file_id"], sheet["file_id"], note["file_id"]], inline_images=False
    )
    assert not any(part.get("type") == "file" for part in parts)
    # The list of every file with its workspace path leads the parts.
    handoff = parts[0]
    assert handoff["type"] == "text" and handoff["text"].startswith(ATTACHED_FILES_HEADER)
    assert handoff["synthetic"] is True
    assert f"- {image['workspace_path']} (image/png, 11 bytes; not readable by the chat model)" in handoff["text"]
    assert f"- {sheet['workspace_path']} (application/vnd.ms-excel, 16 bytes; not readable by the chat model)" in handoff["text"]
    assert f"- {note['workspace_path']} (text/plain, 10 bytes; an excerpt of it is in this prompt)" in handoff["text"]
    assert ATTACHED_FILES_RULE in handoff["text"] and HANDOFF_NO_BROWSER_TOOL in handoff["text"]
    excerpt = next(part for part in parts if part.get("type") == "text" and "hello file" in part.get("text", ""))
    assert f"## notes.txt ({note['workspace_path']})" in excerpt["text"]
    by_id = {entry.get("file_id"): entry for entry in debug}
    assert by_id[image["file_id"]]["action"] == "attachment_handoff" and by_id[image["file_id"]]["inlined"] is False
    assert by_id[image["file_id"]]["path"] == image["workspace_path"]
    assert by_id[sheet["file_id"]]["action"] == "attachment_handoff"
    assert by_id[note["file_id"]]["action"] == "text_context" and by_id[note["file_id"]]["path"] == note["workspace_path"]

    # With a provider that sees images the image is a file part again, listed
    # as such; the spreadsheet is still the one the model cannot read.
    parts, _debug = build_opencode_attachment_parts(svc, "s1", [image["file_id"], sheet["file_id"]], inline_images=True)
    assert any(part.get("type") == "file" and part.get("mime") == "image/png" for part in parts)
    handoff = parts[0]
    assert handoff["text"].startswith(ATTACHED_FILES_HEADER)
    assert f"- {image['workspace_path']} (image/png, 11 bytes; sent to the model as a file part)" in handoff["text"]
    assert f"- {sheet['workspace_path']} (application/vnd.ms-excel, 16 bytes; not readable by the chat model)" in handoff["text"]
    assert HANDOFF_NO_BROWSER_TOOL in handoff["text"]

    # Only readable files: no analysis wording at all.
    parts, _debug = build_opencode_attachment_parts(svc, "s1", [note["file_id"]])
    assert parts[0]["text"].startswith(ATTACHED_FILES_HEADER) and parts[0]["text"].endswith(ATTACHED_FILES_RULE)
    assert HANDOFF_NO_BROWSER_TOOL not in parts[0]["text"]


# --- boot projection and chat -----------------------------------------------------------


def test_boot_projection_records_whether_the_provider_sees_images(monkeypatch):
    monkeypatch.delenv(vision.COPILOT_VISION_VIA_CHAT_ENV, raising=False)
    settings = Settings.from_env()
    result = apply_boot_projection(settings, _payload({"llm": _llm()}))
    assert result.vision_via_chat is False
    assert "image-analysis" not in result.updated_sections
    assert not any(key == "INSPECT_IMAGE_CONFIG" or key.startswith("EFP_INSPECT_IMAGE_") for key in result.env)
    summary = result.public_summary()
    assert summary["vision_via_chat"] is False and "image_analysis" not in summary
    assert not (settings.adapter_state_dir / "inspect-image").exists()

    overlay = ProfileOverlayStore(settings).load()
    assert overlay.vision_via_chat is False
    assert "pw" not in json.dumps(overlay.config)

    # AI Platform chat is multimodal: images stay inline.
    ai = apply_boot_projection(settings, _payload({"llm": _llm(provider="ai_platform")}))
    assert ai.vision_via_chat is True
    assert ProfileOverlayStore(settings).load().vision_via_chat is True


def test_native_projection_copy_carries_no_image_analysis_instructions():
    llm = dict(_llm(), vision={"enabled": True, "model": "gpt-5.4"})
    assert "instruction_texts" not in project_canonical_for_runtime({"llm": llm}, "native")
    assert "instruction_texts" not in project_canonical_for_runtime({"llm": llm}, "opencode")


@pytest.mark.asyncio
async def test_chat_hands_off_the_files_the_provider_cannot_read():
    fake = FakeOpenCodeClient()
    captured = {}

    async def _send_message(session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        captured["parts"] = parts
        return await FakeOpenCodeClient.send_message(
            fake, session_id, parts=parts, model=model, agent=agent, system=system, message_id=message_id, no_reply=no_reply, tools=tools
        )

    fake.send_message = _send_message
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[BOOT_PROJECTION_KEY] = {"ready": True, "error": None, "vision_via_chat": False}
    svc = app[ATTACHMENT_SERVICE_KEY]
    img = svc.upload("s1", "cat.png", PNG_BYTES, "image/png")
    memo = svc.upload("s1", "memo.doc", OLE_BYTES, "application/msword")

    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post(
            "/api/chat",
            json={"message": "what is this?", "session_id": "s1", "attachments": [img["file_id"], memo["file_id"]]},
        )
        assert resp.status == 200
    finally:
        await client.close()

    parts = captured["parts"]
    assert not any(part.get("type") == "file" for part in parts)
    handoff = next(part for part in parts if part.get("type") == "text" and part.get("text", "").startswith(ATTACHED_FILES_HEADER))
    assert img["workspace_path"] in handoff["text"] and memo["workspace_path"] in handoff["text"]
    assert HANDOFF_HEADER in handoff["text"] and HANDOFF_NO_BROWSER_TOOL in handoff["text"]
