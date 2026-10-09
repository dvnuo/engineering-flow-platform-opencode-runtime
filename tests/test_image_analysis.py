"""Image analysis on AI Platform through inspect-image (Copilot vision is off).

- The chat provider decides whether attached images are sent to OpenCode as
  file parts (AI Platform) or described to the agent for the inspect-image CLI
  (GitHub Copilot, unless EFP_COPILOT_VISION_VIA_CHAT says vision is back).
- Boot projects the profile's AI Platform account into a config file of
  inspect-image's own plus child-environment variables, and records it in the
  overlay and the boot snapshot that chat_api reads.
"""
import json

import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter import inspect_image_config as iic
from efp_opencode_adapter.app_keys import ATTACHMENT_SERVICE_KEY, BOOT_PROJECTION_KEY
from efp_opencode_adapter.attachment_service import AttachmentService, build_opencode_attachment_parts
from efp_opencode_adapter.image_handoff import HANDOFF_HOWTO, HANDOFF_NOT_CONFIGURED, build_image_handoff
from efp_opencode_adapter.portal_runtime_context_bootstrap import apply_boot_projection
from efp_opencode_adapter.profile_store import ProfileOverlayStore
from efp_opencode_adapter.runtime_profile_projection import (
    RUNTIME_PROFILE_IMAGE_ANALYSIS_INSTRUCTIONS,
    project_canonical_for_runtime,
)
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


def _vision_llm(provider="github_copilot", enabled=True, vision_model="gpt-5.6-sol", **auth_overrides):
    auth = {"username": "u", "password": "pw", "usercase": "uc"}
    auth.update(auth_overrides)
    llm = {
        "provider": provider,
        "model": "gpt-5.6-terra",
        "api_key": "gho_TEST",
        "ai_platform": {
            "chat": {"host": "https://chat.int", "uri": "/v1/api/v1/chat/completions"},
            "ib2b": {"host": "https://ib2b.int", "uri": "/dsp/token"},
            "auth": {**auth, "trust_token_header": "X-Trust", "tracking_prefix": "EFP"},
        },
    }
    if enabled is not None:
        llm["vision"] = {"enabled": enabled, "model": vision_model}
    return llm


def _payload(config: dict, profile_id="rp-vision", revision=5) -> dict:
    return {"runtime_profile_id": profile_id, "name": "profile", "revision": revision, "config": config}


# --- which provider sees images -------------------------------------------------


def test_chat_provider_accepts_images(monkeypatch):
    monkeypatch.delenv(iic.COPILOT_VISION_VIA_CHAT_ENV, raising=False)
    assert iic.chat_provider_accepts_images({"provider": "ai-platform"})
    assert iic.chat_provider_accepts_images({"provider": "ai_platform"})
    assert not iic.chat_provider_accepts_images({"provider": "github-copilot"})
    assert not iic.chat_provider_accepts_images({})
    monkeypatch.setenv(iic.COPILOT_VISION_VIA_CHAT_ENV, "1")
    assert iic.chat_provider_accepts_images({"provider": "github-copilot"})


def test_resolve_image_analysis_settings_in_the_opencode_form():
    # After the opencode projection the provider and model carry the prefix.
    chat = {"llm": {"provider": "ai-platform", "model": "ai-platform/gpt-5.6-terra", "ai_platform": _vision_llm()["ai_platform"]}}
    settings, reason = iic.resolve_image_analysis_settings(chat)
    assert reason is None and settings.model == "gpt-5.6-terra"

    settings, reason = iic.resolve_image_analysis_settings({"llm": _vision_llm(provider="github-copilot")})
    assert reason is None and settings.model == "gpt-5.6-sol"
    assert settings.chat_uri == "/v1/api/v1/chat/completions" and settings.ib2b_host == "https://ib2b.int"

    _settings, reason = iic.resolve_image_analysis_settings({"llm": _vision_llm(provider="github-copilot", enabled=False)})
    assert reason == "image analysis is off for this profile"
    _settings, reason = iic.resolve_image_analysis_settings({"llm": _vision_llm(password="")})
    assert "usercase" in reason
    assert iic.coerce_vision_model("github-copilot/gpt-5.5") == "gpt-5.4"


def test_write_inspect_image_config_uses_environment_references(monkeypatch):
    monkeypatch.delenv("EFP_MAX_UPLOAD_MB", raising=False)
    settings = Settings.from_env()
    result = iic.write_inspect_image_config(settings, {"llm": _vision_llm()})
    assert result.configured is True
    path = settings.adapter_state_dir / "inspect-image" / "config.yaml"
    assert result.path == str(path)
    text = path.read_text(encoding="utf-8")
    assert "pw" not in text.replace("${EFP_INSPECT_IMAGE_AI_PLATFORM_PASSWORD}", "")
    loaded = yaml.safe_load(text)
    assert loaded["inspect_image"] == {
        "provider": "ai_platform",
        "defaults": {"model": "gpt-5.6-sol"},
        # Whatever the upload API accepts (25 MiB by default) inspect-image must take too.
        "limits": {"max_image_bytes": 25 * 1024 * 1024},
    }
    assert loaded["ai_platform"]["chat"]["host"] == "https://chat.int"
    assert loaded["ai_platform"]["auth"]["password"] == "${EFP_INSPECT_IMAGE_AI_PLATFORM_PASSWORD}"
    assert loaded["ai_platform"]["auth"]["token_file"] == (settings.adapter_state_dir / "inspect-image" / "ai_platform_token").as_posix()
    assert result.env == {
        "INSPECT_IMAGE_CONFIG": str(path),
        "EFP_INSPECT_IMAGE_AI_PLATFORM_USERNAME": "u",
        "EFP_INSPECT_IMAGE_AI_PLATFORM_PASSWORD": "pw",
        "EFP_INSPECT_IMAGE_AI_PLATFORM_USERCASE": "uc",
    }
    assert result.status() == {"configured": True, "reason": None, "model": "gpt-5.6-sol", "config_path": str(path)}

    token_path = settings.adapter_state_dir / "inspect-image" / "ai_platform_token"
    token_path.write_text("old-jwt", encoding="utf-8")
    off = iic.write_inspect_image_config(settings, {"llm": _vision_llm(enabled=False)})
    assert off.configured is False and off.env == {}
    assert off.status()["reason"] == "image analysis is off for this profile"
    assert not path.exists() and not token_path.exists()


def test_inspect_image_size_limit_follows_the_upload_cap(monkeypatch):
    monkeypatch.setenv("EFP_MAX_UPLOAD_MB", "40")
    assert iic.max_image_bytes() == 40 * 1024 * 1024
    settings = Settings.from_env()
    iic.write_inspect_image_config(settings, {"llm": _vision_llm()})
    loaded = yaml.safe_load((settings.adapter_state_dir / "inspect-image" / "config.yaml").read_text(encoding="utf-8"))
    assert loaded["inspect_image"]["limits"]["max_image_bytes"] == 40 * 1024 * 1024
    # A broken value falls back to the upload API's own default, as the server does.
    monkeypatch.setenv("EFP_MAX_UPLOAD_MB", "lots")
    assert iic.max_image_bytes() == 25 * 1024 * 1024


# --- attachment parts -------------------------------------------------------------


def test_attachment_parts_hand_images_to_inspect_image_when_the_model_cannot_see_them():
    settings = Settings.from_env()
    svc = AttachmentService(settings)
    image = svc.upload("s1", "shot.png", b"\x89PNG\r\n\x1a\nabc", "image/png")
    note = svc.upload("s1", "notes.txt", b"hello file", "text/plain")

    parts, debug = build_opencode_attachment_parts(
        svc, "s1", [image["file_id"], note["file_id"]], inline_images=False, image_analysis={"configured": True}
    )
    assert not any(part.get("type") == "file" for part in parts)
    handoff = next(part for part in parts if part.get("type") == "text" and "inspect-image" in part.get("text", ""))
    assert handoff["synthetic"] is True
    assert image["workspace_path"] in handoff["text"]
    assert "(image/png, 11 bytes)" in handoff["text"]
    assert HANDOFF_HOWTO in handoff["text"]
    assert any("hello file" in part.get("text", "") for part in parts if part.get("type") == "text")
    item = next(entry for entry in debug if entry.get("file_id") == image["file_id"])
    assert item["action"] == "inspect_image_handoff" and item["inlined"] is False
    assert item["path"] == image["workspace_path"]

    parts, _debug = build_opencode_attachment_parts(
        svc, "s1", [image["file_id"]], inline_images=False, image_analysis={"configured": False, "reason": "off"}
    )
    assert HANDOFF_NOT_CONFIGURED in next(part["text"] for part in parts if part.get("type") == "text")

    # With a provider that sees images the file part is unchanged.
    parts, _debug = build_opencode_attachment_parts(svc, "s1", [image["file_id"]], inline_images=True)
    assert any(part.get("type") == "file" and part.get("mime") == "image/png" for part in parts)


def test_build_image_handoff_text():
    files = [{"path": "/workspace/uploads/s1/f1/shot.png", "name": "shot.png", "content_type": "image/png", "size_bytes": 12}]
    assert build_image_handoff(files, image_analysis={"configured": True}).startswith("Attached image files.")
    assert build_image_handoff([], image_analysis={"configured": True}) == ""


# --- boot projection and chat -----------------------------------------------------------


def test_boot_projection_configures_inspect_image_for_a_copilot_profile_with_vision(monkeypatch):
    monkeypatch.delenv(iic.COPILOT_VISION_VIA_CHAT_ENV, raising=False)
    settings = Settings.from_env()
    result = apply_boot_projection(settings, _payload({"llm": _vision_llm()}))
    config_path = settings.adapter_state_dir / "inspect-image" / "config.yaml"
    assert config_path.exists()
    assert result.env["INSPECT_IMAGE_CONFIG"] == str(config_path)
    assert result.env["EFP_INSPECT_IMAGE_AI_PLATFORM_PASSWORD"] == "pw"
    assert result.image_analysis["configured"] is True
    assert result.image_analysis["model"] == "gpt-5.6-sol"
    assert result.vision_via_chat is False
    assert "image-analysis" in result.updated_sections
    assert result.public_summary()["image_analysis"]["configured"] is True

    overlay = ProfileOverlayStore(settings).load()
    assert overlay.image_analysis["configured"] is True
    assert overlay.vision_via_chat is False
    assert "pw" not in json.dumps(overlay.config)

    # AI Platform chat: images stay inline and inspect-image uses the chat model.
    ai = apply_boot_projection(settings, _payload({"llm": _vision_llm(provider="ai_platform", enabled=None)}))
    assert ai.vision_via_chat is True
    assert ai.image_analysis == {"configured": True, "reason": None, "model": "gpt-5.6-terra", "config_path": str(config_path)}

    # Off: the file goes away and nothing is exported.
    off = apply_boot_projection(settings, _payload({"llm": _vision_llm(enabled=False)}))
    assert off.image_analysis["configured"] is False
    assert "INSPECT_IMAGE_CONFIG" not in off.env
    assert not config_path.exists()


def test_native_projection_copy_appends_the_image_analysis_instructions():
    native = project_canonical_for_runtime({"llm": _vision_llm()}, "native")
    assert native["instruction_texts"] == [RUNTIME_PROFILE_IMAGE_ANALYSIS_INSTRUCTIONS]
    assert "instruction_texts" not in project_canonical_for_runtime({"llm": _vision_llm()}, "opencode")


@pytest.mark.asyncio
async def test_chat_hands_off_images_when_the_provider_cannot_see_them():
    fake = FakeOpenCodeClient()
    captured = {}

    async def _send_message(session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        captured["parts"] = parts
        return await FakeOpenCodeClient.send_message(
            fake, session_id, parts=parts, model=model, agent=agent, system=system, message_id=message_id, no_reply=no_reply, tools=tools
        )

    fake.send_message = _send_message
    app = create_app(Settings.from_env(), opencode_client=fake)
    app[BOOT_PROJECTION_KEY] = {"ready": True, "error": None, "vision_via_chat": False, "image_analysis": {"configured": True, "model": "gpt-5.4"}}
    svc = app[ATTACHMENT_SERVICE_KEY]
    img = svc.upload("s1", "cat.png", b"\x89PNG\r\n\x1a\nabc", "image/png")

    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/api/chat", json={"message": "what is this?", "session_id": "s1", "attachments": [img["file_id"]]})
        assert resp.status == 200
    finally:
        await client.close()

    parts = captured["parts"]
    assert not any(part.get("type") == "file" for part in parts)
    handoff = next(part for part in parts if part.get("type") == "text" and "inspect-image" in part.get("text", ""))
    assert img["workspace_path"] in handoff["text"]
    assert HANDOFF_HOWTO in handoff["text"]
