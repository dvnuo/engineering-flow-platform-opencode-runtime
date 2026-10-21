"""Generated session titles: the one-shot model call after the first answered turn."""

from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter import session_title
from efp_opencode_adapter.app_keys import (
    OPENCODE_CLIENT_KEY,
    SESSION_STORE_KEY,
    SETTINGS_KEY,
    TASK_BACKGROUND_TASKS_KEY,
    USAGE_TRACKER_KEY,
)
from efp_opencode_adapter.chat_api import _ensure_record_for_chat
from efp_opencode_adapter.portal_metadata_client import PortalMetadataClient
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.session_store import (
    SessionRecord,
    SessionStore,
    session_display_name,
    session_name_source,
    session_title_metadata,
    session_title_metadata_for,
)
from efp_opencode_adapter.session_title import (
    TITLE_INSTRUCTIONS,
    TitleGenerationError,
    TitleTarget,
    build_title_messages,
    build_title_request_body,
    clean_title,
    extract_reply_text,
    first_turn_texts,
    request_title,
    resolve_title_target,
    should_generate_after_turn,
)
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.usage_tracker import TITLE_TASK_TYPE, UsageTracker
from test_t06_helpers import FakeOpenCodeClient


USER_TEXT = "Our release pipeline fails at the deploy stage with a permissions error, how do I fix it?"
ASSISTANT_TEXT = "The deploy stage runs as the jenkins user; grant it the deploy role or run the stage with the service account."
COPILOT_PROFILE = {"config": {"llm": {"provider": "github_copilot", "model": "gpt-6-sol"}}}
AI_PLATFORM_PROFILE = {"config": {"llm": {"provider": "ai_platform", "model": "gpt-5.4"}}}
COPILOT_BASE = "http://127.0.0.1:8000/api/internal/copilot"
AI_PLATFORM_BASE = "http://127.0.0.1:8000/api/internal/ai-platform"


def _record(pid="p1", sid="ses-1", **overrides) -> SessionRecord:
    base = dict(
        portal_session_id=pid,
        opencode_session_id=sid,
        title="first message cut short",
        agent=None,
        model=None,
        created_at="2026-10-20T00:00:00+00:00",
        updated_at="2026-10-20T00:00:00+00:00",
        last_message="",
        message_count=0,
    )
    base.update(overrides)
    return SessionRecord(**base)


def _responses_reply(text='"Release pipeline permissions."', usage=None):
    return {
        "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]},
        ],
        "usage": usage if usage is not None else {"input_tokens": 80, "output_tokens": 6},
    }


def _chat_reply(text="Release pipeline permissions", usage=None):
    return {"choices": [{"message": {"role": "assistant", "content": text}}], "usage": usage if usage is not None else {"prompt_tokens": 80, "completion_tokens": 6}}


# --- cleaning and the request --------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('"Release pipeline permissions"', "Release pipeline permissions"),
        ("Title: Release pipeline permissions.", "Release pipeline permissions"),
        ("“Release   pipeline”\nSecond line ignored", "Release pipeline"),
        ("标题：发布流水线权限问题。", "发布流水线权限问题"),
        ('"Title: Foo"', "Foo"),
        ('"Foo".', "Foo"),
        ("**Foo**", "Foo"),
        ("_Title: “Foo”._", "Foo"),
        ("  \n\n  ", ""),
        (None, ""),
    ],
)
def test_clean_title_strips_wrappers_until_nothing_changes(raw, expected):
    assert clean_title(raw) == expected


def test_clean_title_bounds_the_length():
    cleaned = clean_title("x" * 100)
    assert len(cleaned) == session_title.TITLE_MAX_CHARS
    assert cleaned.endswith("…")


def test_build_title_messages_carry_instructions_and_the_first_turn():
    messages = build_title_messages(USER_TEXT, ASSISTANT_TEXT)
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == TITLE_INSTRUCTIONS
    assert "Reply with the title only" in messages[0]["content"]
    assert USER_TEXT in messages[1]["content"]
    assert ASSISTANT_TEXT in messages[1]["content"]
    assert messages[1]["content"].endswith("Title:")


def test_build_title_messages_cut_long_inputs():
    prompt = build_title_messages("u" * 5000, "a" * 5000)[1]["content"]
    assert "u" * session_title.TITLE_INPUT_MAX_CHARS + "…" in prompt
    assert "u" * (session_title.TITLE_INPUT_MAX_CHARS + 1) not in prompt


def test_request_body_per_family():
    copilot = build_title_request_body(TitleTarget("github-copilot", "gpt-6-luna", COPILOT_BASE), user_text=USER_TEXT, assistant_text=ASSISTANT_TEXT)
    assert copilot == {
        "model": "gpt-6-luna",
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": TITLE_INSTRUCTIONS}]},
            {"role": "user", "content": [{"type": "input_text", "text": build_title_messages(USER_TEXT, ASSISTANT_TEXT)[1]["content"]}]},
        ],
        "stream": False,
        "reasoning": {"effort": "low"},
        "max_output_tokens": session_title.TITLE_MAX_OUTPUT_TOKENS,
    }
    ai_platform = build_title_request_body(TitleTarget("ai-platform", "gpt-5.6-luna", AI_PLATFORM_BASE), user_text=USER_TEXT, assistant_text=ASSISTANT_TEXT)
    assert ai_platform == {"model": "gpt-5.6-luna", "messages": build_title_messages(USER_TEXT, ASSISTANT_TEXT), "stream": False}
    assert "reasoning" not in ai_platform and "max_output_tokens" not in ai_platform


@pytest.mark.parametrize(
    "payload, api, expected",
    [
        (_responses_reply("A title"), "responses", "A title"),
        ({"output": [{"type": "message", "content": [{"type": "output_text", "text": "Part "}, {"type": "refusal", "refusal": "no"}, {"type": "output_text", "text": "two"}]}]}, "responses", "Part two"),
        ({"output": []}, "responses", ""),
        (_chat_reply("A title"), "chat", "A title"),
        ({"choices": [{"message": {"content": [{"type": "text", "text": "Part "}, {"type": "text", "text": "two"}]}}]}, "chat", "Part two"),
        ({"choices": [{"text": "legacy completion"}]}, "chat", ""),
        ({"content": "top level"}, "chat", ""),
        ({"choices": []}, "chat", ""),
        ("nope", "responses", ""),
    ],
)
def test_extract_reply_text(payload, api, expected):
    assert extract_reply_text(payload, api=api) == expected


# --- which proxy, API and model ----------------------------------------------------


def _settings(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings.from_env()


def test_resolve_title_target_for_copilot(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch, EFP_COPILOT_PROXY_BASE_URL=COPILOT_BASE + "/")
    target = resolve_title_target(settings, COPILOT_PROFILE)
    assert target == TitleTarget(provider="github-copilot", model="gpt-6-luna", base_url=COPILOT_BASE)
    assert target.api == "responses"
    assert target.url == COPILOT_BASE + "/responses"


def test_resolve_title_target_for_ai_platform_and_overrides(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch, EFP_AI_PLATFORM_PROXY_BASE_URL=AI_PLATFORM_BASE)
    target = resolve_title_target(settings, AI_PLATFORM_PROFILE)
    assert (target.provider, target.model, target.api) == ("ai-platform", "gpt-5.6-luna", "chat")
    assert target.url == AI_PLATFORM_BASE + "/chat/completions"

    overridden = resolve_title_target(settings, {"config": {"llm": {"provider": "ai_platform", "title_model": "ai-platform/gpt-5.4"}}})
    assert overridden.model == "gpt-5.4"
    # A top-level provider on the profile works when the config block is absent.
    assert resolve_title_target(settings, {"provider": "copilot"}).provider == "github-copilot"


@pytest.mark.parametrize(
    "title_model",
    ["auto", "auto-efficiency", "github-copilot/auto-intelligence", "ai-platform/gpt-5.4", "  "],
    ids=["auto", "auto tier", "prefixed auto", "another family", "blank"],
)
def test_resolve_title_target_falls_back_to_the_family_default(tmp_path, monkeypatch, title_model, caplog):
    settings = _settings(tmp_path, monkeypatch)
    profile = {"config": {"llm": {"provider": "github_copilot", "title_model": title_model}}}
    assert resolve_title_target(settings, profile).model == "gpt-6-luna"


def test_resolve_title_target_unknown_provider_or_no_proxy(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    assert resolve_title_target(settings, {"config": {"llm": {"provider": "anthropic"}}}) is None
    assert resolve_title_target(settings, None) is None  # nothing configured anywhere
    without_proxy = dataclasses.replace(settings, copilot_proxy_base_url="")
    assert resolve_title_target(without_proxy, COPILOT_PROFILE) is None  # the proxy base URL is empty


def test_resolve_title_target_falls_back_to_the_pods_profile_overlay(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    overlay = settings.adapter_state_dir / "runtime-profile-overlay.json"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(json.dumps({"config": {"llm": {"provider": "github_copilot", "title_model": "gpt-6-sol"}}}), encoding="utf-8")

    target = resolve_title_target(settings, None)
    assert target.provider == "github-copilot"
    assert target.model == "gpt-6-sol"
    # A request profile without a provider still borrows the overlay's.
    assert resolve_title_target(settings, {"config": {"llm": {"reasoning_effort": "low"}}}).provider == "github-copilot"


# --- the call through the loopback proxy ------------------------------------------


class _FakeProxy:
    """A fake loopback proxy: records what the adapter sent and answers like the family's API."""

    def __init__(self, family="github-copilot", reply=None, *, status=200, delay=0.0, raw_body=None):
        self.family = family
        self.reply = reply if reply is not None else (_responses_reply() if family == "github-copilot" else _chat_reply())
        self.status = status
        self.delay = delay
        self.raw_body = raw_body
        self.requests = []
        self.server = None
        self.base_url = ""

    async def handle(self, request):
        self.requests.append({"path": request.path, "body": await request.json(), "auth": request.headers.get("Authorization")})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raw_body is not None:
            return web.Response(status=self.status, text=self.raw_body, content_type="text/plain")
        return web.json_response(self.reply, status=self.status)

    async def start(self):
        app = web.Application()
        app.router.add_post("/{tail:.*}", self.handle)
        self.server = TestServer(app)
        await self.server.start_server()
        leaf = "api/internal/copilot" if self.family == "github-copilot" else "api/internal/ai-platform"
        self.base_url = str(self.server.make_url(f"/{leaf}")).rstrip("/")
        return self.base_url

    async def close(self):
        if self.server is not None:
            await self.server.close()

    def assert_one_request(self, *, model, user_text, assistant_text):
        """The exact path and body shape for the family, so a wrong endpoint cannot regress."""

        assert len(self.requests) == 1
        sent = self.requests[0]
        assert sent["auth"] is None  # the proxy adds the member's credential itself
        target = TitleTarget(self.family, model, self.base_url)
        assert sent["path"] == ("/api/internal/copilot/responses" if self.family == "github-copilot" else "/api/internal/ai-platform/chat/completions")
        assert sent["body"] == build_title_request_body(target, user_text=user_text, assistant_text=assistant_text)


@pytest.fixture
async def copilot_proxy():
    proxy = _FakeProxy("github-copilot")
    await proxy.start()
    yield proxy
    await proxy.close()


@pytest.fixture
async def ai_platform_proxy():
    proxy = _FakeProxy("ai-platform")
    await proxy.start()
    yield proxy
    await proxy.close()


async def test_request_title_posts_a_responses_call_for_copilot(copilot_proxy):
    target = TitleTarget("github-copilot", "gpt-6-luna", copilot_proxy.base_url)

    text, usage = await request_title(target, user_text=USER_TEXT, assistant_text=ASSISTANT_TEXT)

    assert text == '"Release pipeline permissions."'
    assert usage == {"input_tokens": 80, "output_tokens": 6}
    copilot_proxy.assert_one_request(model="gpt-6-luna", user_text=USER_TEXT, assistant_text=ASSISTANT_TEXT)
    assert copilot_proxy.requests[0]["body"]["reasoning"] == {"effort": "low"}


async def test_request_title_posts_a_chat_completion_for_ai_platform(ai_platform_proxy):
    target = TitleTarget("ai-platform", "gpt-5.6-luna", ai_platform_proxy.base_url)

    text, usage = await request_title(target, user_text=USER_TEXT, assistant_text=ASSISTANT_TEXT)

    assert text == "Release pipeline permissions"
    assert usage == {"prompt_tokens": 80, "completion_tokens": 6}
    ai_platform_proxy.assert_one_request(model="gpt-5.6-luna", user_text=USER_TEXT, assistant_text=ASSISTANT_TEXT)
    assert "reasoning" not in ai_platform_proxy.requests[0]["body"]


async def test_request_title_maps_provider_failures():
    failing = _FakeProxy(reply={"error": "rate limited"}, status=429)
    base_url = await failing.start()
    try:
        with pytest.raises(TitleGenerationError) as excinfo:
            await request_title(TitleTarget("github-copilot", "gpt-6-luna", base_url), user_text="u", assistant_text="a")
        assert excinfo.value.status_code == 502
        assert "429" in excinfo.value.message
    finally:
        await failing.close()

    garbage = _FakeProxy(raw_body="<html>not json</html>")
    base_url = await garbage.start()
    try:
        with pytest.raises(TitleGenerationError) as excinfo:
            await request_title(TitleTarget("github-copilot", "gpt-6-luna", base_url), user_text="u", assistant_text="a")
        assert excinfo.value.status_code == 502
    finally:
        await garbage.close()


async def test_request_title_times_out():
    slow = _FakeProxy(delay=0.5)
    base_url = await slow.start()
    try:
        with pytest.raises(TitleGenerationError) as excinfo:
            await request_title(TitleTarget("github-copilot", "gpt-6-luna", base_url), user_text="u", assistant_text="a", timeout=0.05)
        assert excinfo.value.status_code == 504
    finally:
        await slow.close()


async def test_request_title_unreachable_proxy_is_a_502():
    # A server that was listening and is gone: the connection is refused at once.
    gone = _FakeProxy()
    base_url = await gone.start()
    await gone.close()
    with pytest.raises(TitleGenerationError) as excinfo:
        await request_title(TitleTarget("github-copilot", "gpt-6-luna", base_url), user_text="u", assistant_text="a", timeout=5)
    assert excinfo.value.status_code == 502


# --- the first turn's texts (manual endpoint path) --------------------------------------


def test_first_turn_texts_reads_the_first_answered_turn():
    messages = [
        {"id": "u1", "role": "user", "parts": [{"type": "text", "text": USER_TEXT}]},
        {"id": "a0", "role": "assistant", "parts": []},
        {"id": "a1", "role": "assistant", "parts": [{"type": "text", "text": ASSISTANT_TEXT}]},
        {"id": "u2", "role": "user", "parts": [{"type": "text", "text": "later"}]},
    ]
    assert first_turn_texts(messages) == (USER_TEXT, ASSISTANT_TEXT)
    assert first_turn_texts(messages[:2]) == (USER_TEXT, "")
    assert first_turn_texts([]) == ("", "")


def test_first_turn_texts_prefers_the_members_own_words():
    class _Display:
        def get_user_message(self, *, opencode_session_id, opencode_message_id, portal_session_id=None):
            assert (opencode_session_id, opencode_message_id, portal_session_id) == ("ses-1", "u1", "p1")
            return {"display_content": "What does this CSV show?"}

    messages = [
        {"id": "u1", "role": "user", "parts": [{"type": "text", "text": "[attachment context]\n\nWhat does this CSV show?"}]},
        {"id": "a1", "role": "assistant", "parts": [{"type": "text", "text": "Monthly revenue by region."}]},
    ]
    assert first_turn_texts(messages, display_store=_Display(), portal_session_id="p1", opencode_session_id="ses-1") == (
        "What does this CSV show?",
        "Monthly revenue by region.",
    )


# --- the record ---------------------------------------------------------------------


def test_display_name_precedence_and_source():
    derived = _record()
    assert session_display_name(derived) == "first message cut short"
    assert session_name_source(derived) == "derived"
    assert session_title_metadata(derived) == {}
    generated = _record(generated_title="Release pipeline permissions")
    assert session_display_name(generated) == "Release pipeline permissions"
    assert session_name_source(generated) == "generated"
    assert session_title_metadata(generated) == {"session_title": "Release pipeline permissions"}
    custom = _record(generated_title="Release pipeline permissions", custom_title="My notes")
    assert session_display_name(custom) == "My notes"
    assert session_name_source(custom) == "custom"
    assert session_title_metadata(custom) == {"session_title": "My notes"}
    empty = _record(title="")
    assert session_display_name(empty) == "Chat"
    assert session_name_source(empty) == "none"
    assert session_title_metadata(None) == {}


def test_session_title_metadata_for_reads_the_store(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    store.upsert(_record(generated_title="Generated"))
    assert session_title_metadata_for(store, "p1") == {"session_title": "Generated"}
    assert session_title_metadata_for(store, "missing") == {}
    assert session_title_metadata_for(store, "") == {}
    assert session_title_metadata_for(None, "p1") == {}


def test_store_persists_titles_and_loads_old_records(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    store.upsert(_record())
    # A record written before titles existed has neither key.
    index = json.loads(store.index_path.read_text(encoding="utf-8"))
    del index["sessions"]["p1"]["custom_title"]
    del index["sessions"]["p1"]["generated_title"]
    store.index_path.write_text(json.dumps(index), encoding="utf-8")

    reloaded = SessionStore(tmp_path / "sessions")
    old = reloaded.get("p1")
    assert old.custom_title is None and old.generated_title is None
    assert session_name_source(old) == "derived"

    sorted_at = old.updated_at
    generated = reloaded.set_generated_title("p1", "  Release pipeline permissions ")
    assert generated.generated_title == "Release pipeline permissions"
    assert generated.title == "first message cut short"
    assert generated.updated_at == sorted_at  # a background title does not move the session up the list
    renamed = reloaded.rename("p1", "My notes")
    assert renamed.custom_title == "My notes"
    assert renamed.title == "My notes"
    assert renamed.generated_title == "Release pipeline permissions"
    assert renamed.updated_at != sorted_at

    moved = reloaded.replace_opencode_session_after_mutation("p1", "ses-2", message_count=4)
    assert (moved.custom_title, moved.generated_title) == ("My notes", "Release pipeline permissions")
    again = SessionStore(tmp_path / "sessions").get("p1")
    assert (again.custom_title, again.generated_title) == ("My notes", "Release pipeline permissions")


async def test_partial_recovery_keeps_the_titles(tmp_path):
    class _GoneClient(FakeOpenCodeClient):
        async def get_session(self, session_id):
            from efp_opencode_adapter.opencode_client import OpenCodeClientError

            raise OpenCodeClientError("not found", status=404)

    store = SessionStore(tmp_path / "sessions")
    store.upsert(_record(custom_title="My notes", generated_title="Generated", last_message="hi", message_count=4))

    recovered, partial = await _ensure_record_for_chat(client=_GoneClient(), store=store, portal_session_id="p1", title="new title", agent="build", model="m")

    assert partial is True
    assert recovered.opencode_session_id == "ses-1"
    assert (recovered.custom_title, recovered.generated_title) == ("My notes", "Generated")
    assert (recovered.last_message, recovered.message_count, recovered.agent, recovered.model) == ("hi", 4, "build", "m")
    assert recovered.partial_recovery is True and recovered.deleted is False
    assert store.get("p1").custom_title == "My notes"


# --- when the background call runs ------------------------------------------------------


def _app(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())


def test_should_generate_after_a_real_answered_turn(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    assert should_generate_after_turn(app, _record(), ok=True, assistant_text="an answer") is True
    # Not only the first turn: a session whose earlier turns failed is still unnamed.
    assert should_generate_after_turn(app, _record(message_count=4), ok=True, assistant_text="an answer") is True


@pytest.mark.parametrize(
    "record, ok, assistant_text",
    [
        (None, True, "a"),
        (_record(), False, "a"),
        (_record(), True, "   "),
        (_record(custom_title="Mine"), True, "a"),
        (_record(generated_title="Done"), True, "a"),
        (_record(deleted=True), True, "a"),
    ],
    ids=["no record", "not ok (incomplete, blocked or synthetic)", "no answer text", "manual name", "already generated", "deleted"],
)
def test_should_not_generate(tmp_path, monkeypatch, record, ok, assistant_text):
    app = _app(tmp_path, monkeypatch)
    assert should_generate_after_turn(app, record, ok=ok, assistant_text=assistant_text) is False


async def test_should_not_generate_while_a_title_task_runs(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    release = asyncio.Event()

    async def pending():
        await release.wait()

    task = asyncio.create_task(pending(), name=session_title.title_task_name("p1"))
    app[TASK_BACKGROUND_TASKS_KEY].add(task)
    try:
        assert should_generate_after_turn(app, _record(), ok=True, assistant_text="a") is False
        assert session_title.schedule_session_title(app, "p1", user_text="u", assistant_text="a", runtime_profile=COPILOT_PROFILE) is None
    finally:
        release.set()
        await task


async def test_background_task_is_quiet_without_a_provider(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    task = session_title.schedule_session_title(app, "p1", user_text="u", assistant_text="a", runtime_profile=None)
    assert task is not None  # the target is resolved inside the task, off the request path
    await task
    assert app[TASK_BACKGROUND_TASKS_KEY] == set()


# --- end to end through the adapter -----------------------------------------------------


async def _app_client(tmp_path, monkeypatch, proxy, opencode_client=None):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    env_key = "EFP_COPILOT_PROXY_BASE_URL" if proxy.family == "github-copilot" else "EFP_AI_PLATFORM_PROXY_BASE_URL"
    monkeypatch.setenv(env_key, proxy.base_url)
    app = create_app(Settings.from_env(), opencode_client=opencode_client or FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    return app, client


async def _settle_background(app):
    tasks = list(app[TASK_BACKGROUND_TASKS_KEY])
    if tasks:
        await asyncio.gather(*tasks)


def _usage_rows(app, row_type):
    rows = [json.loads(line) for line in app[USAGE_TRACKER_KEY].usage_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [row for row in rows if row["type"] == row_type]


async def test_first_turn_names_the_session_and_the_name_is_everywhere(tmp_path, monkeypatch, copilot_proxy):
    app, client = await _app_client(tmp_path, monkeypatch, copilot_proxy)
    try:
        chat = await client.post("/api/chat", json={"message": USER_TEXT, "session_id": "s1", "metadata": {"runtime_profile": COPILOT_PROFILE}})
        assert (await chat.json())["completion_state"] == "completed"
        await _settle_background(app)

        copilot_proxy.assert_one_request(model="gpt-6-luna", user_text=USER_TEXT, assistant_text=f"echo: {USER_TEXT}")

        name = await (await client.get("/api/sessions/s1/name")).json()
        assert name == {"session_id": "s1", "name": "Release pipeline permissions", "name_source": "generated"}
        listing = await (await client.get("/api/sessions")).json()
        assert listing["sessions"][0]["name"] == "Release pipeline permissions"
        assert listing["sessions"][0]["name_source"] == "generated"
        detail = await (await client.get("/api/sessions/s1")).json()
        assert detail["name"] == "Release pipeline permissions"
        assert detail["name_source"] == "generated"

        title_rows = _usage_rows(app, TITLE_TASK_TYPE)
        assert title_rows and title_rows[0]["model"] == "gpt-6-luna" and title_rows[0]["input_tokens"] == 80

        # A named session does not ask again.
        await client.post("/api/chat", json={"message": "and then?", "session_id": "s1", "metadata": {"runtime_profile": COPILOT_PROFILE}})
        await _settle_background(app)
        assert len(copilot_proxy.requests) == 1
    finally:
        await client.close()


async def test_ai_platform_first_turn_uses_chat_completions(tmp_path, monkeypatch, ai_platform_proxy):
    ai_platform_proxy.reply = _chat_reply("Deploy stage permissions", usage={})
    app, client = await _app_client(tmp_path, monkeypatch, ai_platform_proxy)
    try:
        await client.post("/api/chat", json={"message": USER_TEXT, "session_id": "s1", "metadata": {"runtime_profile": AI_PLATFORM_PROFILE}})
        await _settle_background(app)
        ai_platform_proxy.assert_one_request(model="gpt-5.6-luna", user_text=USER_TEXT, assistant_text=f"echo: {USER_TEXT}")
        assert (await (await client.get("/api/sessions/s1/name")).json())["name"] == "Deploy stage permissions"
        # No usage block in the reply: the call is still one request with zero tokens.
        rows = _usage_rows(app, TITLE_TASK_TYPE)
        assert rows and rows[0]["requests"] == 1 and rows[0]["input_tokens"] == 0 and rows[0]["provider"] == "ai-platform"
    finally:
        await client.close()


async def test_a_synthetic_or_failed_turn_does_not_name_the_session(tmp_path, monkeypatch, copilot_proxy):
    class _OnlyUserClient(FakeOpenCodeClient):
        async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
            user = {"id": message_id or "u-only", "role": "user", "parts": parts}
            self.messages[session_id].append(user)
            return {"message": user}

    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "0.2")
    app, client = await _app_client(tmp_path, monkeypatch, copilot_proxy, opencode_client=_OnlyUserClient())
    try:
        chat = await client.post("/api/chat", json={"message": USER_TEXT, "session_id": "s1", "metadata": {"runtime_profile": COPILOT_PROFILE}})
        assert (await chat.json())["ok"] is False
        await _settle_background(app)
        assert copilot_proxy.requests == []
        assert (await (await client.get("/api/sessions/s1/name")).json())["name_source"] == "derived"
    finally:
        await client.close()


async def test_rename_wins_tells_the_portal_and_regenerate_respects_it(tmp_path, monkeypatch, copilot_proxy):
    portal = _Portal()
    base = await portal.start()
    monkeypatch.setenv("PORTAL_INTERNAL_BASE_URL", base)
    monkeypatch.setenv("PORTAL_AGENT_ID", "agent-1")
    app, client = await _app_client(tmp_path, monkeypatch, copilot_proxy)
    try:
        await client.post("/api/chat", json={"message": USER_TEXT, "session_id": "s1", "metadata": {"runtime_profile": COPILOT_PROFILE}})
        await _settle_background(app)

        renamed = await (await client.post("/api/sessions/s1/rename", json={"name": "My notes"})).json()
        assert renamed == {"success": True, "session_id": "s1", "name": "My notes", "name_source": "custom"}
        await _settle_background(app)
        patches = [call["body"] for call in portal.calls if call["method"] == "PATCH"]
        assert patches == [{"title": "Release pipeline permissions"}, {"title": "My notes"}]
        assert (await (await client.get("/api/sessions/s1/name")).json())["name_source"] == "custom"

        regenerated = await (await client.post("/api/sessions/s1/title/generate", json={"force": True})).json()
        assert regenerated == {"success": True, "generated": False, "session_id": "s1", "name": "My notes", "name_source": "custom"}
        assert len(copilot_proxy.requests) == 1  # the model was not asked again
    finally:
        await client.close()
        await portal.close()


async def test_generate_endpoint_regenerates_only_when_forced(tmp_path, monkeypatch, copilot_proxy):
    app, client = await _app_client(tmp_path, monkeypatch, copilot_proxy)
    overlay = app[SETTINGS_KEY].adapter_state_dir / "runtime-profile-overlay.json"
    overlay.write_text(json.dumps({"config": {"llm": {"provider": "github_copilot"}}}), encoding="utf-8")
    try:
        await client.post("/api/chat", json={"message": USER_TEXT, "session_id": "s1", "metadata": {"runtime_profile": COPILOT_PROFILE}})
        await _settle_background(app)
        assert len(copilot_proxy.requests) == 1
        sorted_at = app[SESSION_STORE_KEY].get("s1").updated_at

        kept = await (await client.post("/api/sessions/s1/title/generate")).json()
        assert kept["generated"] is False and kept["name_source"] == "generated"
        assert len(copilot_proxy.requests) == 1

        copilot_proxy.reply = _responses_reply("Deploy stage permissions")
        forced = await (await client.post("/api/sessions/s1/title/generate", json={"force": True})).json()
        assert forced == {"success": True, "generated": True, "session_id": "s1", "name": "Deploy stage permissions", "name_source": "generated"}
        assert len(copilot_proxy.requests) == 2
        assert app[SESSION_STORE_KEY].get("s1").updated_at == sorted_at
        await _settle_background(app)
    finally:
        await client.close()


async def test_generate_endpoint_waits_for_the_running_background_call(tmp_path, monkeypatch):
    slow = _FakeProxy(delay=0.3)
    await slow.start()
    app, client = await _app_client(tmp_path, monkeypatch, slow)
    try:
        await client.post("/api/chat", json={"message": USER_TEXT, "session_id": "s1", "metadata": {"runtime_profile": COPILOT_PROFILE}})
        assert session_title.find_title_task(app, "s1") is not None
        forced = await (await client.post("/api/sessions/s1/title/generate", json={"force": True})).json()
        assert forced == {"success": True, "generated": True, "session_id": "s1", "name": "Release pipeline permissions", "name_source": "generated"}
        assert len(slow.requests) == 1  # one model call for the session, not two
        await _settle_background(app)
    finally:
        await client.close()
        await slow.close()


async def test_generate_endpoint_error_statuses(tmp_path, monkeypatch, copilot_proxy):
    class _OnlyUserClient(FakeOpenCodeClient):
        async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
            user = {"id": message_id or "u-only", "role": "user", "parts": parts}
            self.messages[session_id].append(user)
            return {"message": user}

    app, client = await _app_client(tmp_path, monkeypatch, copilot_proxy, opencode_client=_OnlyUserClient())
    overlay = app[SETTINGS_KEY].adapter_state_dir / "runtime-profile-overlay.json"
    overlay.write_text(json.dumps({"config": {"llm": {"provider": "github_copilot"}}}), encoding="utf-8")
    try:
        missing = await client.post("/api/sessions/nope/title/generate", json={})
        assert missing.status == 404
        assert (await client.get("/api/sessions/nope/name")).status == 404

        store = app[SESSION_STORE_KEY]
        fake = app[OPENCODE_CLIENT_KEY]
        created = await fake.create_session(title="x")
        fake.messages[created["id"]] = [{"id": "u1", "role": "user", "parts": [{"type": "text", "text": "hello"}]}]
        store.upsert(_record(pid="s-user-only", sid=created["id"]))

        malformed = await client.post("/api/sessions/s-user-only/title/generate", json={"metadata": "nope"})
        assert malformed.status == 400

        unanswered = await client.post("/api/sessions/s-user-only/title/generate", json={"force": True})
        assert unanswered.status == 422
        assert "no completed turn" in (await unanswered.json())["error"]

        fake.messages[created["id"]].append({"id": "a1", "role": "assistant", "parts": [{"type": "text", "text": "hi"}]})
        copilot_proxy.status = 500
        copilot_proxy.reply = {"error": "boom"}
        failed = await client.post("/api/sessions/s-user-only/title/generate", json={"force": True})
        assert failed.status == 502

        async def timed_out(*_args, **_kwargs):
            raise TitleGenerationError("Title generation timed out after 30s.", status_code=504)

        monkeypatch.setattr(session_title, "request_title", timed_out)
        slow = await client.post("/api/sessions/s-user-only/title/generate", json={"force": True})
        assert slow.status == 504
        assert (await (await client.get("/api/sessions/s-user-only/name")).json())["name_source"] == "derived"
    finally:
        await client.close()


async def test_generate_endpoint_without_a_provider_is_a_502(tmp_path, monkeypatch, copilot_proxy):
    app, client = await _app_client(tmp_path, monkeypatch, copilot_proxy)
    try:
        await client.post("/api/chat", json={"message": USER_TEXT, "session_id": "s1"})
        await _settle_background(app)
        assert copilot_proxy.requests == []  # no runtime profile and no overlay: nothing to call
        resp = await client.post("/api/sessions/s1/title/generate", json={"force": True})
        assert resp.status == 502
        assert "No model proxy" in (await resp.json())["error"]
    finally:
        await client.close()


async def test_a_rename_during_generation_wins(tmp_path, monkeypatch):
    slow = _FakeProxy(delay=0.3)
    await slow.start()
    app, client = await _app_client(tmp_path, monkeypatch, slow)
    try:
        await client.post("/api/chat", json={"message": USER_TEXT, "session_id": "s1", "metadata": {"runtime_profile": COPILOT_PROFILE}})
        await asyncio.sleep(0.05)
        await client.post("/api/sessions/s1/rename", json={"name": "Renamed meanwhile"})
        await _settle_background(app)
        record = app[SESSION_STORE_KEY].get("s1")
        assert record.custom_title == "Renamed meanwhile"
        assert record.generated_title is None
    finally:
        await client.close()
        await slow.close()


async def test_background_failure_leaves_the_derived_name(tmp_path, monkeypatch):
    failing = _FakeProxy(reply={"error": "boom"}, status=500)
    await failing.start()
    app, client = await _app_client(tmp_path, monkeypatch, failing)
    try:
        chat = await client.post("/api/chat", json={"message": USER_TEXT, "session_id": "s1", "metadata": {"runtime_profile": COPILOT_PROFILE}})
        assert chat.status == 200
        await _settle_background(app)
        name = await (await client.get("/api/sessions/s1/name")).json()
        assert name["name_source"] == "derived"
        assert session_title.find_title_task(app, "s1") is None
    finally:
        await client.close()
        await failing.close()


# --- the Portal learns the name ---------------------------------------------------------


class _Portal:
    def __init__(self, status=200):
        self.status = status
        self.calls = []
        self.server = None

    async def handle(self, request):
        self.calls.append({"method": request.method, "raw_path": request.raw_path, "body": await request.json(), "auth": request.headers.get("Authorization")})
        return web.json_response({"ok": True}, status=self.status)

    async def start(self):
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        self.server = TestServer(app)
        await self.server.start_server()
        return str(self.server.make_url("")).rstrip("/")

    async def close(self):
        if self.server is not None:
            await self.server.close()


async def test_publish_session_title_patches_the_portal(monkeypatch):
    portal = _Portal()
    base = await portal.start()
    try:
        monkeypatch.setenv("PORTAL_INTERNAL_BASE_URL", base)
        monkeypatch.setenv("PORTAL_AGENT_ID", "agent 1")
        monkeypatch.setenv("PORTAL_INTERNAL_TOKEN", "tok")
        out = await PortalMetadataClient(Settings.from_env()).publish_session_title(session_id="s/1", title="  Release pipeline permissions ")
        assert out == {"success": True, "status": 200}
        assert portal.calls == [
            {
                "method": "PATCH",
                "raw_path": "/api/internal/agents/agent%201/sessions/s%2F1/title",
                "body": {"title": "Release pipeline permissions"},
                "auth": "Bearer tok",
            }
        ]
    finally:
        await portal.close()


async def test_publish_session_title_skips_a_portal_without_the_endpoint(monkeypatch, tmp_path):
    portal = _Portal(status=404)
    base = await portal.start()
    pending = tmp_path / "pending.jsonl"
    try:
        monkeypatch.setenv("PORTAL_INTERNAL_BASE_URL", base)
        monkeypatch.setenv("PORTAL_AGENT_ID", "agent")
        out = await PortalMetadataClient(Settings.from_env(), pending_file=pending).publish_session_title(session_id="s1", title="x")
        assert out == {"success": False, "skipped": True, "status": 404, "reason": "portal_title_endpoint_unavailable"}
        assert not pending.exists()
    finally:
        await portal.close()


async def test_publish_session_title_logs_and_records_other_failures(monkeypatch, tmp_path, caplog):
    portal = _Portal(status=500)
    base = await portal.start()
    pending = tmp_path / "pending.jsonl"
    try:
        monkeypatch.setenv("PORTAL_INTERNAL_BASE_URL", base)
        monkeypatch.setenv("PORTAL_AGENT_ID", "agent")
        with caplog.at_level("WARNING", logger="efp_opencode_adapter.portal_metadata_client"):
            out = await PortalMetadataClient(Settings.from_env(), pending_file=pending).publish_session_title(session_id="s1", title="x")
        assert out["success"] is False and out["status"] == 500
        assert any("Session title publish failed" in message for message in caplog.messages)
        recorded = json.loads(pending.read_text(encoding="utf-8").splitlines()[0])
        assert recorded["method"] == "PATCH" and recorded["url"].endswith("/sessions/s1/title") and recorded["payload"] == {"title": "x"}
    finally:
        await portal.close()
    monkeypatch.delenv("PORTAL_INTERNAL_BASE_URL", raising=False)
    monkeypatch.delenv("PORTAL_AGENT_ID", raising=False)
    assert await PortalMetadataClient(Settings.from_env()).publish_session_title(session_id="s1", title="x") == {"success": False, "skipped": True}
    monkeypatch.setenv("PORTAL_INTERNAL_BASE_URL", "http://portal")
    monkeypatch.setenv("PORTAL_AGENT_ID", "agent")
    assert await PortalMetadataClient(Settings.from_env()).publish_session_title(session_id="s1", title="   ") == {"success": False, "skipped": True}


async def test_the_name_travels_with_every_portal_publish(tmp_path, monkeypatch, copilot_proxy):
    portal = _Portal()
    base = await portal.start()
    monkeypatch.setenv("PORTAL_INTERNAL_BASE_URL", base)
    monkeypatch.setenv("PORTAL_AGENT_ID", "agent-1")
    app, client = await _app_client(tmp_path, monkeypatch, copilot_proxy)
    try:
        await client.post("/api/chat", json={"message": USER_TEXT, "session_id": "s1", "metadata": {"runtime_profile": COPILOT_PROFILE}})
        await _settle_background(app)
        patches = [call for call in portal.calls if call["method"] == "PATCH"]
        assert patches == [
            {"method": "PATCH", "raw_path": "/api/internal/agents/agent-1/sessions/s1/title", "body": {"title": "Release pipeline permissions"}, "auth": None}
        ]
        # The next turn's chat.started and chat.completed publishes carry the name too.
        before = len(portal.calls)
        await client.post("/api/chat", json={"message": "more", "session_id": "s1", "metadata": {"runtime_profile": COPILOT_PROFILE}})
        puts = [call for call in portal.calls[before:] if call["method"] == "PUT"]
        assert len(puts) == 2
        for put in puts:
            assert json.loads(put["body"]["metadata_json"])["session_title"] == "Release pipeline permissions"
    finally:
        await client.close()
        await portal.close()


def test_usage_tracker_records_title_calls(tmp_path):
    tracker = UsageTracker(tmp_path / "usage.jsonl")
    row = tracker.record_session_title(session_id="s1", model="gpt-6-luna", provider="github-copilot", usage={"input_tokens": 80, "output_tokens": 6})
    assert row["type"] == TITLE_TASK_TYPE and row["input_tokens"] == 80 and row["output_tokens"] == 6 and row["messages"] == 0
    empty = tracker.record_session_title(session_id="s1", model="gpt-6-luna", provider="github-copilot", usage={})
    assert empty["requests"] == 1 and empty["input_tokens"] == 0 and empty["output_tokens"] == 0
    chat = tracker.record_chat(session_id="s1", request_id="r1", model=None, provider=None, response_payload={"usage": {"prompt_tokens": 3, "completion_tokens": 4}, "model": "m", "provider": "p"}, input_text="i", output_text="o")
    assert (chat["type"], chat["model"], chat["provider"], chat["messages"], chat["input_tokens"], chat["output_tokens"]) == ("chat", "m", "p", 2, 3, 4)
    summary = tracker.summarize(days=1)
    assert summary["global"]["total_requests"] == 3
    assert {row["model"] for row in summary["by_model"]} == {"gpt-6-luna", "m"}
