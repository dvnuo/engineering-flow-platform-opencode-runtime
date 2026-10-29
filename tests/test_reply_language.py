"""The member's reply language: block selection and rendering, and where the block lands.

The Portal sends ``metadata.portal_user.reply_language`` (``en``, ``zh-Hans``
or ``zh-Hant-HK``) and ``reply_language_strict`` with every proxied request.
The block leads the per-turn ``system`` string that OpenCode appends to its
prompt, and switches the session-title language rule. The Chinese
parentheticals are written as escapes: source files in this repository stay
ASCII.
"""

from __future__ import annotations

import asyncio
import json
import tomllib
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter import reply_language as reply_language_module
from efp_opencode_adapter.app_keys import TASK_BACKGROUND_TASKS_KEY
from efp_opencode_adapter.chat_api import FINAL_RESPONSE_CONTRACT_SUFFIX
from efp_opencode_adapter.reply_language import (
    REPLY_LANGUAGE_TEXTS_PATH,
    ReplyLanguage,
    prepend_reply_language_block,
    render_reply_language_block,
    resolve_reply_language,
)
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.session_title import (
    TITLE_INSTRUCTIONS,
    TitleTarget,
    build_title_messages,
    build_title_request_body,
    title_instructions,
)
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


SIMPLIFIED = "\u7b80\u4f53\u4e2d\u6587"
HONG_KONG = (
    "\u7e41\u9ad4\u4e2d\u6587\uff0c\u9999\u6e2f\u7528\u8a9e\uff1a"
    "\u8edf\u4ef6\u3001\u4f3a\u670d\u5668\u3001\u7db2\u7d61\u3001\u767b\u5165\u3001\u8a2d\u5b9a\u3001\u9810\u8a2d"
)
DISPLAY_NAMES = {
    "en": "English",
    "zh-Hans": "Simplified Chinese",
    "zh-Hant-HK": "Traditional Chinese (Hong Kong)",
}
LANGUAGE_SENTENCES = {
    "en": "Write your prose in English.",
    "zh-Hans": f"Write your prose in Simplified Chinese as used in mainland China ({SIMPLIFIED}).",
    "zh-Hant-HK": (
        "Write your prose in Traditional Chinese characters using Standard Written Chinese "
        f"with Hong Kong vocabulary ({HONG_KONG}), not vernacular Cantonese and not Taiwan vocabulary."
    ),
}
SOFT_SENTENCE = (
    "This is the member's default, taken from their Portal language rather than chosen for replies. "
    "When the member clearly writes to you in another language, or asks for a particular reply language "
    "in this chat or in a saved note, reply in that language instead. "
    "Messages the Portal sends on the member's behalf, such as \"Continue\", a request to fix a diagram "
    "or a notice from a panel, do not change the language."
)
STRICT_SENTENCE = (
    "Always reply in {display_name}, even when the user writes in another language, "
    "unless the user explicitly asks you to switch. "
    "The member chose this in the Portal, so it wins over earlier notes about reply language."
)
BLOCK_TAIL = (
    "Keep code, commands, file paths, identifiers, error text, product names and established technical terms "
    "exactly as they are, usually in English.\n"
    "Everything you write into a repository or an external system stays in English unless the user asks "
    "otherwise: code and comments, commit messages, pull request titles and descriptions, documentation, "
    "tickets and wiki pages.\n"
    "This setting comes from the member's Portal language preference."
)
TODAYS_SYSTEM = FINAL_RESPONSE_CONTRACT_SUFFIX.strip()
COPILOT_PROFILE = {"config": {"llm": {"provider": "github_copilot", "model": "gpt-6-sol"}}}


def expected_block(tag: str, strict: bool) -> str:
    display_name = DISPLAY_NAMES[tag]
    mode = STRICT_SENTENCE.format(display_name=display_name) if strict else SOFT_SENTENCE
    return f"Reply language: {display_name}.\n{LANGUAGE_SENTENCES[tag]}\n{mode}\n{BLOCK_TAIL}"


# --- selection -----------------------------------------------------------------


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        "portal_user",
        {},
        {"title": "x"},
        {"portal_user": None},
        {"portal_user": "zh-Hans"},
        {"portal_user": ["zh-Hans"]},
        {"portal_user": {}},
        {"portal_user": {"id": "u1", "display_name": "Ann"}},
        {"portal_user": {"reply_language": None}},
        {"portal_user": {"reply_language": 1}},
        {"portal_user": {"reply_language": ["zh-Hans"]}},
        {"portal_user": {"reply_language": "", "reply_language_strict": True}},
        {"portal_user": {"reply_language": "fr", "reply_language_strict": True}},
        {"portal_user": {"reply_language": "zh", "reply_language_strict": True}},
        {"portal_user": {"reply_language": "ZH-HANS"}},
        {"portal_user": {"reply_language": "en"}},
        {"portal_user": {"reply_language": "en", "reply_language_strict": False}},
        {"portal_user": {"reply_language": "en", "reply_language_strict": "true"}},
        {"portal_user": {"reply_language": "en", "reply_language_strict": 1}},
    ],
)
def test_no_block_for_absent_unknown_or_default_english(metadata):
    assert resolve_reply_language(metadata) is None


@pytest.mark.parametrize(
    ("portal_user", "expected"),
    [
        ({"reply_language": "en", "reply_language_strict": True}, ReplyLanguage("en", True)),
        ({"reply_language": "zh-Hans"}, ReplyLanguage("zh-Hans", False)),
        ({"reply_language": "zh-Hans", "reply_language_strict": False}, ReplyLanguage("zh-Hans", False)),
        ({"reply_language": "zh-Hans", "reply_language_strict": True}, ReplyLanguage("zh-Hans", True)),
        ({"reply_language": "zh-Hant-HK", "reply_language_strict": False}, ReplyLanguage("zh-Hant-HK", False)),
        ({"reply_language": "zh-Hant-HK", "reply_language_strict": True}, ReplyLanguage("zh-Hant-HK", True)),
        # Only a JSON true is strict; surrounding whitespace in the tag is tolerated.
        ({"reply_language": " zh-Hant-HK ", "reply_language_strict": "yes"}, ReplyLanguage("zh-Hant-HK", False)),
    ],
)
def test_block_selected(portal_user, expected):
    assert resolve_reply_language({"portal_user": {"id": "u1", **portal_user}, "title": "x"}) == expected


# --- rendering -----------------------------------------------------------------


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("tag", ["en", "zh-Hans", "zh-Hant-HK"])
def test_render_block_text(tag, strict):
    block = render_reply_language_block(ReplyLanguage(tag, strict))

    assert block == expected_block(tag, strict)
    assert block.count("\n") == 5 and not block.endswith("\n")
    assert ReplyLanguage(tag, strict).display_name == DISPLAY_NAMES[tag]


def test_chinese_sentences_carry_the_native_characters():
    assert SIMPLIFIED in render_reply_language_block(ReplyLanguage("zh-Hans", False))
    assert HONG_KONG in render_reply_language_block(ReplyLanguage("zh-Hant-HK", True))
    assert len(SIMPLIFIED) == 4 and len(HONG_KONG) == 28


def test_prepend_block_keeps_todays_string_without_a_language():
    zh = ReplyLanguage("zh-Hans", False)

    assert prepend_reply_language_block("Be terse.", None) == "Be terse."
    assert prepend_reply_language_block(None, None) is None
    assert prepend_reply_language_block("Be terse.", zh) == expected_block("zh-Hans", False) + "\n\nBe terse."
    assert prepend_reply_language_block(None, zh) == expected_block("zh-Hans", False)
    assert prepend_reply_language_block("   ", zh) == expected_block("zh-Hans", False)


def test_texts_file_is_ascii_escaped_and_packaged():
    raw = REPLY_LANGUAGE_TEXTS_PATH.read_bytes()
    assert raw.isascii()
    assert b"\\u7b80\\u4f53\\u4e2d\\u6587" in raw
    assert Path(reply_language_module.__file__).read_bytes().isascii()
    assert Path(__file__).read_bytes().isascii()
    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert "prompts/*.json" in pyproject["tool"]["setuptools"]["package-data"]["efp_opencode_adapter"]


# --- the per-turn system string ------------------------------------------------


class _SystemCaptureClient(FakeOpenCodeClient):
    """Records the system string and parts of every prompt the adapter sends."""

    def __init__(self):
        super().__init__()
        self.calls = []

    async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
        self.calls.append({"session_id": session_id, "system": system, "parts": parts})
        return await super().send_message(
            session_id, parts=parts, model=model, agent=agent, system=system, message_id=message_id, no_reply=no_reply, tools=tools
        )

    @property
    def systems(self):
        return [call["system"] for call in self.calls]


async def _chat_app(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    opencode = _SystemCaptureClient()
    app = create_app(Settings.from_env(), opencode_client=opencode)
    client = TestClient(TestServer(app))
    await client.start_server()
    return app, client, opencode


async def _settle_background(app):
    tasks = list(app[TASK_BACKGROUND_TASKS_KEY])
    if tasks:
        await asyncio.gather(*tasks)


async def test_chat_system_string_is_unchanged_without_a_reply_language(tmp_path, monkeypatch):
    app, client, opencode = await _chat_app(tmp_path, monkeypatch)
    try:
        for metadata in (
            {},
            {"portal_user": {"id": "u1", "reply_language": "en", "reply_language_strict": False}},
            {"portal_user": {"id": "u1", "reply_language": "fr", "reply_language_strict": True}},
        ):
            resp = await client.post("/api/chat", json={"message": "hello", "session_id": "s1", "metadata": metadata})
            assert resp.status == 200
        assert opencode.systems == [TODAYS_SYSTEM] * 3

        resp = await client.post("/api/chat", json={"message": "hello", "session_id": "s1", "metadata": {"system": "Be terse."}})
        assert resp.status == 200
        resp = await client.post("/api/chat", json={"message": "hello", "session_id": "s1", "system": "Be brief."})
        assert resp.status == 200
        assert opencode.systems[3:] == ["Be terse." + FINAL_RESPONSE_CONTRACT_SUFFIX, "Be brief." + FINAL_RESPONSE_CONTRACT_SUFFIX]
    finally:
        await client.close()


async def test_chat_system_string_leads_with_the_block(tmp_path, monkeypatch):
    app, client, opencode = await _chat_app(tmp_path, monkeypatch)
    try:
        strict_hk = {"id": "u1", "reply_language": "zh-Hant-HK", "reply_language_strict": True}
        resp = await client.post("/api/chat", json={"message": "hello", "session_id": "s1", "metadata": {"portal_user": strict_hk}})
        assert resp.status == 200
        assert (await resp.json())["response"] == "echo: hello"
        assert opencode.systems == [expected_block("zh-Hant-HK", True) + "\n\n" + TODAYS_SYSTEM]

        resp = await client.post(
            "/api/chat",
            json={"message": "hello", "session_id": "s1", "system": "Be terse.", "metadata": {"portal_user": {"reply_language": "zh-Hans"}}},
        )
        assert resp.status == 200
        assert opencode.systems[-1] == expected_block("zh-Hans", False) + "\n\nBe terse." + FINAL_RESPONSE_CONTRACT_SUFFIX

        resp = await client.post(
            "/api/chat",
            json={"message": "hello", "session_id": "s1", "metadata": {"portal_user": {"reply_language": "en", "reply_language_strict": True}}},
        )
        assert resp.status == 200
        assert opencode.systems[-1] == expected_block("en", True) + "\n\n" + TODAYS_SYSTEM

        # The block is a system string, never a message part: parts feed the
        # Copilot auto-routing prompt and the first-message conversation key.
        for call in opencode.calls:
            assert [part["text"] for part in call["parts"]] == ["hello"]
    finally:
        await client.close()


async def test_stream_chat_carries_the_block(tmp_path, monkeypatch):
    app, client, opencode = await _chat_app(tmp_path, monkeypatch)
    try:
        resp = await client.post(
            "/api/chat/stream",
            json={"message": "hello", "session_id": "s1", "metadata": {"portal_user": {"reply_language": "zh-Hans", "reply_language_strict": True}}},
        )
        assert resp.status == 200
        await resp.read()
        assert opencode.systems == [expected_block("zh-Hans", True) + "\n\n" + TODAYS_SYSTEM]
    finally:
        await client.close()


async def test_slash_skill_prompt_carries_the_block(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    skill = {"efp_name": "collect_requirements_to_bundle", "opencode_name": "collect-requirements-to-bundle", "description": "Collect requirements"}
    (state / "skills-index.json").write_text(json.dumps({"skills": [skill]}), encoding="utf-8")
    app, client, opencode = await _chat_app(tmp_path, monkeypatch)
    try:
        resp = await client.post(
            "/api/chat",
            json={"message": "/collect-requirements-to-bundle for the login page", "session_id": "s1", "metadata": {"portal_user": {"reply_language": "zh-Hans"}}},
        )
        assert resp.status == 200, await resp.text()
        call = opencode.calls[-1]
        # The skill prompt replaced the member's text as a synthetic part; the
        # block still travels in the system string of that send.
        assert call["parts"][0].get("synthetic") is True
        assert call["parts"][0]["metadata"]["efp_internal"] == "skill_prompt"
        assert call["system"] == expected_block("zh-Hans", False) + "\n\n" + TODAYS_SYSTEM
    finally:
        await client.close()


async def test_synchronous_edit_prepends_the_block_to_the_bodys_own_system(tmp_path, monkeypatch):
    app, client, opencode = await _chat_app(tmp_path, monkeypatch)
    zh = {"reply_language": "zh-Hans", "reply_language_strict": True}
    try:
        cases = [
            ("s1", {"content": "hello again"}, None),
            ("s2", {"content": "hello again", "system": "Be terse."}, "Be terse."),
            ("s3", {"content": "hello again", "metadata": {"portal_user": zh}}, expected_block("zh-Hans", True)),
            ("s4", {"content": "hello again", "system": "Be terse.", "metadata": {"portal_user": zh}}, expected_block("zh-Hans", True) + "\n\nBe terse."),
        ]
        for session_id, body, expected_system in cases:
            chat = await client.post("/api/chat", json={"message": "hello", "session_id": session_id})
            assert chat.status == 200
            edit = await client.post(f"/api/sessions/{session_id}/messages/u-1/edit", json=body)
            assert edit.status == 200, await edit.text()
            assert opencode.calls[-1]["system"] == expected_system
            assert [part["text"] for part in opencode.calls[-1]["parts"]] == ["hello again"]
    finally:
        await client.close()


async def test_asynchronous_edit_goes_through_the_chat_path_with_the_block(tmp_path, monkeypatch):
    app, client, opencode = await _chat_app(tmp_path, monkeypatch)
    try:
        chat = await client.post("/api/chat", json={"message": "hello", "session_id": "s1"})
        assert chat.status == 200
        edit = await client.post(
            "/api/sessions/s1/messages/u-1/edit/async",
            json={"content": "hello again", "metadata": {"portal_user": {"reply_language": "zh-Hant-HK"}}},
        )
        assert edit.status == 202, await edit.text()
        await _settle_background(app)
        assert opencode.calls[-1]["system"] == expected_block("zh-Hant-HK", False) + "\n\n" + TODAYS_SYSTEM
        assert [part["text"] for part in opencode.calls[-1]["parts"]] == ["hello again"]
    finally:
        await client.close()


# --- session titles ------------------------------------------------------------


def test_title_instructions_follow_the_answer_only_with_a_block():
    assert title_instructions(None) == TITLE_INSTRUCTIONS
    assert "- The same language as the user's message.\n" in TITLE_INSTRUCTIONS

    for reply_language in (ReplyLanguage("en", True), ReplyLanguage("zh-Hans", False), ReplyLanguage("zh-Hant-HK", True)):
        variant = title_instructions(reply_language)
        assert "- The same language as the assistant's answer.\n" in variant
        assert "user's message" not in variant
        # Only the language rule changes; the rest of the instructions stay.
        assert variant.replace("- The same language as the assistant's answer.", "- The same language as the user's message.", 1) == TITLE_INSTRUCTIONS


def test_title_request_bodies_carry_the_variant():
    hk = ReplyLanguage("zh-Hant-HK", True)
    copilot = TitleTarget("github-copilot", "gpt-6-luna", "http://127.0.0.1:8000/api/internal/copilot")
    ai_platform = TitleTarget("ai-platform", "gpt-5.6-luna", "http://127.0.0.1:8000/api/internal/ai-platform")

    assert build_title_messages("u", "a", reply_language=hk)[0] == {"role": "system", "content": title_instructions(hk)}
    assert build_title_messages("u", "a")[0] == {"role": "system", "content": TITLE_INSTRUCTIONS}

    responses_body = build_title_request_body(copilot, user_text="u", assistant_text="a", reply_language=hk)
    assert responses_body["input"][0]["content"][0]["text"] == title_instructions(hk)
    chat_body = build_title_request_body(ai_platform, user_text="u", assistant_text="a", reply_language=hk)
    assert chat_body["messages"][0]["content"] == title_instructions(hk)

    # Without the setting both bodies are today's.
    for target in (copilot, ai_platform):
        assert build_title_request_body(target, user_text="u", assistant_text="a") == build_title_request_body(
            target, user_text="u", assistant_text="a", reply_language=None
        )
    assert build_title_request_body(copilot, user_text="u", assistant_text="a")["input"][0]["content"][0]["text"] == TITLE_INSTRUCTIONS


class _CopilotTitleProxy:
    """A fake Copilot loopback proxy that records the title calls' instructions."""

    def __init__(self):
        self.instructions = []
        self.server = None
        self.base_url = ""

    async def handle(self, request):
        body = await request.json()
        self.instructions.append(body["input"][0]["content"][0]["text"])
        reply = {
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Named"}]}],
            "usage": {"input_tokens": 8, "output_tokens": 2},
        }
        return web.json_response(reply)

    async def start(self):
        app = web.Application()
        app.router.add_post("/{tail:.*}", self.handle)
        self.server = TestServer(app)
        await self.server.start_server()
        self.base_url = str(self.server.make_url("/api/internal/copilot")).rstrip("/")

    async def close(self):
        if self.server is not None:
            await self.server.close()


async def test_title_calls_follow_the_turns_reply_language(tmp_path, monkeypatch):
    proxy = _CopilotTitleProxy()
    await proxy.start()
    monkeypatch.setenv("EFP_COPILOT_PROXY_BASE_URL", proxy.base_url)
    app, client, _ = await _chat_app(tmp_path, monkeypatch)
    try:
        # The after-turn call carries the chat's reply language.
        resp = await client.post(
            "/api/chat",
            json={"message": "hello", "session_id": "s1", "metadata": {"runtime_profile": COPILOT_PROFILE, "portal_user": {"reply_language": "zh-Hans"}}},
        )
        assert resp.status == 200
        await _settle_background(app)
        assert proxy.instructions == [title_instructions(ReplyLanguage("zh-Hans", False))]
        assert (await (await client.get("/api/sessions/s1/name")).json())["name"] == "Named"

        resp = await client.post("/api/chat", json={"message": "hello", "session_id": "s2", "metadata": {"runtime_profile": COPILOT_PROFILE}})
        assert resp.status == 200
        await _settle_background(app)
        assert proxy.instructions[1] == TITLE_INSTRUCTIONS

        # POST .../title/generate reads the reply language from its own body.
        forced = await client.post(
            "/api/sessions/s2/title/generate",
            json={"force": True, "metadata": {"runtime_profile": COPILOT_PROFILE, "portal_user": {"reply_language": "zh-Hant-HK", "reply_language_strict": True}}},
        )
        assert forced.status == 200, await forced.text()
        assert (await forced.json())["generated"] is True
        assert proxy.instructions[2] == title_instructions(ReplyLanguage("zh-Hant-HK", True))

        forced = await client.post("/api/sessions/s2/title/generate", json={"force": True, "metadata": {"runtime_profile": COPILOT_PROFILE}})
        assert forced.status == 200, await forced.text()
        assert proxy.instructions[3] == TITLE_INSTRUCTIONS
        await _settle_background(app)
    finally:
        await client.close()
        await proxy.close()


def test_a_missing_texts_file_keeps_todays_prompt(monkeypatch, tmp_path):
    monkeypatch.setattr(reply_language_module, "REPLY_LANGUAGE_TEXTS_PATH", tmp_path / "missing.json")
    reply_language_module._texts.cache_clear()
    try:
        metadata = {"portal_user": {"id": "77", "reply_language": "zh-Hans", "reply_language_strict": True}}
        assert reply_language_module.resolve_reply_language(metadata) is None
    finally:
        reply_language_module._texts.cache_clear()
