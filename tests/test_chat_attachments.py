import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.app_keys import ATTACHMENT_SERVICE_KEY
from efp_opencode_adapter.chat_api import _redact_attachment_payloads_for_debug
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.thinking_events import safe_preview
from test_t06_helpers import FakeOpenCodeClient


class CapturingFakeOpenCodeClient(FakeOpenCodeClient):
    def __init__(self):
        super().__init__()
        self.last_parts = None

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        self.last_parts = parts
        return await super().send_message(session_id, parts=parts, model=model, agent=agent, system=system, message_id=message_id, no_reply=no_reply, tools=tools)


@pytest.mark.asyncio
async def test_chat_attachments_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    fake = CapturingFakeOpenCodeClient()
    app = create_app(Settings.from_env(), opencode_client=fake)
    svc = app[ATTACHMENT_SERVICE_KEY]

    txt = svc.upload("s1", "notes.txt", b"hello file", "text/plain")
    img = svc.upload("s1", "cat.png", b"\x89PNG\r\n\x1a\nabc", "image/png")
    pdf = svc.upload("s1", "doc.pdf", b"%PDF-1.4\nabc", "application/pdf")
    docx = svc.upload("s1", "report.docx", b"PK\x03\x04abc", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        r = await client.post("/api/chat", json={"message": "read it", "session_id": "s1", "attachments": [txt["file_id"]]})
        assert r.status == 200
        assert any(p.get("type") == "text" and "hello file" in p.get("text", "") and "notes.txt" in p.get("text", "") for p in fake.last_parts)

        r2 = await client.post("/api/chat", json={"message": "read it", "session_id": "s1", "attachments": [{"file_id": txt["file_id"], "name": "x.txt", "content_type": "bad/type", "size": 1}]})
        assert r2.status == 200
        assert any(p.get("type") == "text" and "hello file" in p.get("text", "") for p in fake.last_parts)

        r3 = await client.post("/api/chat", json={"message": "image", "session_id": "s1", "attachments": [img["file_id"]]})
        p3 = await r3.json()
        fp = next(p for p in fake.last_parts if p.get("type") == "file")
        assert fp["mime"] == "image/png"
        assert fp["filename"] == "cat.png"
        assert fp["url"].startswith("data:image/png;base64,")
        assert "base64," not in json.dumps(p3["_llm_debug"]) 

        r4 = await client.post("/api/chat", json={"message": "pdf", "session_id": "s1", "attachments": [pdf["file_id"]]})
        assert r4.status == 200
        fp2 = next(p for p in fake.last_parts if p.get("type") == "file")
        assert fp2["mime"] == "application/pdf"
        assert fp2["url"].startswith("data:application/pdf;base64,")

        r5 = await client.post("/api/chat", json={"message": "docx", "session_id": "s1", "attachments": [docx["file_id"]]})
        assert r5.status == 200
        assert any("cannot parse or inline this file type yet" in p.get("text", "") for p in fake.last_parts if p.get("type") == "text")
        assert not any(p.get("type") == "file" for p in fake.last_parts)

        r6 = await client.post("/api/chat", json={"message": "missing", "session_id": "s1", "attachments": ["does-not-exist"]})
        assert r6.status == 200
        p6 = await r6.json()
        assert any("could not be loaded" in p.get("text", "") for p in fake.last_parts if p.get("type") == "text")
        assert any(d.get("status") == "error" for d in p6["_llm_debug"]["attachments"])
    finally:
        await client.close()


def test_response_payload_redaction():
    payload = {"message": {"parts": [{"type": "file", "url": "data:image/png;base64,AAAA"}]}, "note": "see data:application/pdf;base64,BBBB"}
    redacted = _redact_attachment_payloads_for_debug(payload)
    preview = safe_preview(redacted, 2000)
    preview_text = json.dumps(preview, ensure_ascii=False)
    assert "data:image/png;base64,AAAA" not in preview_text
    assert "data:application/pdf;base64,BBBB" not in preview_text
    assert "data:<redacted>" in preview_text
    assert redacted["message"]["parts"][0]["url"] == "data:<redacted>"


def test_response_payload_redaction_svg_and_plain_text():
    payload = {"note": "x data:image/svg+xml;base64,AAAA y"}
    redacted = _redact_attachment_payloads_for_debug(payload)
    preview_text = json.dumps(safe_preview(redacted, 2000), ensure_ascii=False)
    assert "AAAA" not in preview_text
    assert "data:<redacted>;base64,<redacted>" in preview_text

    plain = {"text": "this is not data url"}
    assert _redact_attachment_payloads_for_debug(plain) == plain
