"""Generated session titles: the one-shot model call after the first answered turn."""

from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter import session_title
from efp_opencode_adapter.app_keys import OPENCODE_CLIENT_KEY, SESSION_STORE_KEY, SETTINGS_KEY, TASK_BACKGROUND_TASKS_KEY, USAGE_TRACKER_KEY
from efp_opencode_adapter.portal_metadata_client import PortalMetadataClient
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.session_store import (
    SessionRecord,
    SessionStore,
    session_display_name,
    session_name_source,
)
from efp_opencode_adapter.session_title import (
    TitleGenerationError,
    TitleTarget,
    build_title_messages,
    clean_title,
    extract_completion_text,
    first_turn_texts,
    request_title,
    resolve_title_target,
    should_generate_after_turn,
)
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.usage_tracker import UsageTracker
from test_t06_helpers import FakeOpenCodeClient


USER_TEXT = "Our release pipeline fails at the deploy stage with a permissions error, how do I fix it?"
ASSISTANT_TEXT = "The deploy stage runs as the jenkins user; grant it the deploy role or run the stage with the service account."
COPILOT_PROFILE = {"config": {"llm": {"provider": "github_copilot", "model": "gpt-6-sol"}}}


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


@pytest.fixture(autouse=True)
def _clear_inflight():
    session_title._inflight.clear()
    yield
    session_title._inflight.clear()


# --- cleaning and the request --------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('"Release pipeline permissions"', "Release pipeline permissions"),
        ("Title: Release pipeline permissions.", "Release pipeline permissions"),
        ("“Release   pipeline”\nSecond line ignored", "Release pipeline"),
        ("标题：发布流水线权限问题。", "发布流水线权限问题"),
        ("  \n\n  ", ""),
        (None, ""),
    ],
)
def test_clean_title_strips_quotes_prefixes_and_periods(raw, expected):
    assert clean_title(raw) == expected


def test_clean_title_bounds_the_length():
    cleaned = clean_title("x" * 100)
    assert len(cleaned) == session_title.TITLE_MAX_CHARS
    assert cleaned.endswith("…")


def test_build_title_messages_carry_instructions_and_the_first_turn():
    messages = build_title_messages(USER_TEXT, ASSISTANT_TEXT)
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == session_title.TITLE_INSTRUCTIONS
    assert "Reply with the title only" in messages[0]["content"]
    assert USER_TEXT in messages[1]["content"]
    assert ASSISTANT_TEXT in messages[1]["content"]
    assert messages[1]["content"].endswith("Title:")


def test_build_title_messages_cut_long_inputs():
    prompt = build_title_messages("u" * 5000, "a" * 5000)[1]["content"]
    assert "u" * session_title.TITLE_INPUT_MAX_CHARS + "…" in prompt
    assert "u" * (session_title.TITLE_INPUT_MAX_CHARS + 1) not in prompt


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"choices": [{"message": {"content": "A title"}}]}, "A title"),
        ({"choices": [{"message": {"content": [{"type": "text", "text": "Part "}, {"type": "text", "text": "two"}]}}]}, "Part two"),
        ({"choices": [{"text": "legacy"}]}, "legacy"),
        ({"output_text": "responses"}, "responses"),
        ({"choices": []}, ""),
        ("nope", ""),
    ],
)
def test_extract_completion_text(payload, expected):
    assert extract_completion_text(payload) == expected


# --- which proxy and model ---------------------------------------------------------


def _settings(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings.from_env()


def test_resolve_title_target_for_copilot(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch, EFP_COPILOT_PROXY_BASE_URL="http://127.0.0.1:8000/api/internal/copilot/")
    target = resolve_title_target(settings, COPILOT_PROFILE)
    assert target == TitleTarget(provider="github-copilot", model="gpt-6-luna", base_url="http://127.0.0.1:8000/api/internal/copilot")
    assert target.url == "http://127.0.0.1:8000/api/internal/copilot/chat/completions"


def test_resolve_title_target_for_ai_platform_and_overrides(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch, EFP_AI_PLATFORM_PROXY_BASE_URL="http://127.0.0.1:8000/api/internal/ai-platform")
    target = resolve_title_target(settings, {"config": {"llm": {"provider": "ai_platform"}}})
    assert target.provider == "ai-platform"
    assert target.model == "gpt-5.6-luna"
    assert target.url == "http://127.0.0.1:8000/api/internal/ai-platform/chat/completions"

    overridden = resolve_title_target(settings, {"config": {"llm": {"provider": "ai_platform", "title_model": "ai-platform/gpt-5.4"}}})
    assert overridden.model == "gpt-5.4"
    # A top-level provider on the profile works when the config block is absent.
    assert resolve_title_target(settings, {"provider": "copilot"}).provider == "github-copilot"


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


class _TitleProxy:
    """A fake loopback proxy: records what the adapter sent and answers like chat/completions."""

    def __init__(self, reply=None, *, status=200, delay=0.0, raw_body=None):
        self.reply = reply or {"choices": [{"message": {"content": '"Release pipeline permissions."'}}], "usage": {"prompt_tokens": 80, "completion_tokens": 6}}
        self.status = status
        self.delay = delay
        self.raw_body = raw_body
        self.requests = []
        self.server = None

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
        return str(self.server.make_url("/api/internal/copilot")).rstrip("/")

    async def close(self):
        if self.server is not None:
            await self.server.close()


@pytest.fixture
async def title_proxy():
    proxy = _TitleProxy()
    proxy.base_url = await proxy.start()
    yield proxy
    await proxy.close()


async def test_request_title_posts_a_chat_completion_and_reads_the_reply(title_proxy):
    target = TitleTarget(provider="github-copilot", model="gpt-6-luna", base_url=title_proxy.base_url)

    text, usage = await request_title(target, user_text=USER_TEXT, assistant_text=ASSISTANT_TEXT)

    assert text == '"Release pipeline permissions."'
    assert usage == {"prompt_tokens": 80, "completion_tokens": 6}
    sent = title_proxy.requests[0]
    assert sent["path"] == "/api/internal/copilot/chat/completions"
    assert sent["auth"] is None  # the proxy adds the member's credential itself
    assert sent["body"]["model"] == "gpt-6-luna"
    assert sent["body"]["stream"] is False
    assert sent["body"]["messages"] == build_title_messages(USER_TEXT, ASSISTANT_TEXT)


async def test_request_title_maps_provider_failures():
    failing = _TitleProxy(reply={"error": "rate limited"}, status=429)
    base_url = await failing.start()
    try:
        with pytest.raises(TitleGenerationError) as excinfo:
            await request_title(TitleTarget("github-copilot", "gpt-6-luna", base_url), user_text="u", assistant_text="a")
        assert excinfo.value.status_code == 502
        assert "429" in excinfo.value.message
    finally:
        await failing.close()

    garbage = _TitleProxy(raw_body="<html>not json</html>")
    base_url = await garbage.start()
    try:
        with pytest.raises(TitleGenerationError) as excinfo:
            await request_title(TitleTarget("github-copilot", "gpt-6-luna", base_url), user_text="u", assistant_text="a")
        assert excinfo.value.status_code == 502
    finally:
        await garbage.close()


async def test_request_title_times_out():
    slow = _TitleProxy(delay=0.5)
    base_url = await slow.start()
    try:
        with pytest.raises(TitleGenerationError) as excinfo:
            await request_title(TitleTarget("github-copilot", "gpt-6-luna", base_url), user_text="u", assistant_text="a", timeout=0.05)
        assert excinfo.value.status_code == 504
    finally:
        await slow.close()


async def test_request_title_unreachable_proxy_is_a_502():
    # A server that was listening and is gone: the connection is refused at once.
    gone = _TitleProxy()
    base_url = await gone.start()
    await gone.close()
    with pytest.raises(TitleGenerationError) as excinfo:
        await request_title(TitleTarget("github-copilot", "gpt-6-luna", base_url), user_text="u", assistant_text="a", timeout=5)
    assert excinfo.value.status_code == 502


# --- the first turn's texts -------------------------------------------------------


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
    generated = _record(generated_title="Release pipeline permissions")
    assert session_display_name(generated) == "Release pipeline permissions"
    assert session_name_source(generated) == "generated"
    custom = _record(generated_title="Release pipeline permissions", custom_title="My notes")
    assert session_display_name(custom) == "My notes"
    assert session_name_source(custom) == "custom"
    empty = _record(title="")
    assert session_display_name(empty) == "Chat"
    assert session_name_source(empty) == "none"


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

    generated = reloaded.set_generated_title("p1", "  Release pipeline permissions ")
    assert generated.generated_title == "Release pipeline permissions"
    assert generated.title == "first message cut short"
    renamed = reloaded.rename("p1", "My notes")
    assert renamed.custom_title == "My notes"
    assert renamed.title == "My notes"
    assert renamed.generated_title == "Release pipeline permissions"

    moved = reloaded.replace_opencode_session_after_mutation("p1", "ses-2", message_count=4)
    assert (moved.custom_title, moved.generated_title) == ("My notes", "Release pipeline permissions")
    again = SessionStore(tmp_path / "sessions").get("p1")
    assert (again.custom_title, again.generated_title) == ("My notes", "Release pipeline permissions")


# --- when the background call runs ------------------------------------------------------


def test_should_generate_after_the_first_answered_turn():
    assert should_generate_after_turn(_record(), completion_state="completed", assistant_text="an answer") is True


@pytest.mark.parametrize(
    "record, completion_state, assistant_text",
    [
        (None, "completed", "a"),
        (_record(pid="task-1"), "completed", "a"),
        (_record(pid="delegation:1"), "completed", "a"),
        (_record(), "incomplete", "a"),
        (_record(), "completed", "   "),
        (_record(message_count=2), "completed", "a"),
        (_record(custom_title="Mine"), "completed", "a"),
        (_record(generated_title="Done"), "completed", "a"),
        (_record(deleted=True), "completed", "a"),
    ],
    ids=["no record", "task session", "delegation session", "not completed", "no answer text", "second turn", "manual name", "already generated", "deleted"],
)
def test_should_not_generate(record, completion_state, assistant_text):
    assert should_generate_after_turn(record, completion_state=completion_state, assistant_text=assistant_text) is False


async def test_schedule_is_quiet_without_a_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    assert session_title.schedule_session_title(app, "p1", runtime_profile=None) is None
    assert app[TASK_BACKGROUND_TASKS_KEY] == set()


# --- end to end through the adapter -----------------------------------------------------


async def _app_client(tmp_path, monkeypatch, proxy_base_url, opencode_client=None):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_COPILOT_PROXY_BASE_URL", proxy_base_url)
    app = create_app(Settings.from_env(), opencode_client=opencode_client or FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    return app, client


async def _settle_background(app):
    tasks = list(app[TASK_BACKGROUND_TASKS_KEY])
    if tasks:
        await asyncio.gather(*tasks)


async def test_first_turn_names_the_session_and_the_name_is_everywhere(tmp_path, monkeypatch, title_proxy):
    app, client = await _app_client(tmp_path, monkeypatch, title_proxy.base_url)
    try:
        chat = await client.post("/api/chat", json={"message": USER_TEXT, "session_id": "s1", "metadata": {"runtime_profile": COPILOT_PROFILE}})
        assert (await chat.json())["completion_state"] == "completed"
        await _settle_background(app)

        assert len(title_proxy.requests) == 1
        assert title_proxy.requests[0]["body"]["model"] == "gpt-6-luna"
        assert USER_TEXT in title_proxy.requests[0]["body"]["messages"][1]["content"]

        name = await (await client.get("/api/sessions/s1/name")).json()
        assert name == {"session_id": "s1", "name": "Release pipeline permissions", "name_source": "generated"}
        listing = await (await client.get("/api/sessions")).json()
        assert listing["sessions"][0]["name"] == "Release pipeline permissions"
        assert listing["sessions"][0]["name_source"] == "generated"
        detail = await (await client.get("/api/sessions/s1")).json()
        assert detail["name"] == "Release pipeline permissions"
        assert detail["name_source"] == "generated"

        usage_rows = [json.loads(line) for line in app[USAGE_TRACKER_KEY].usage_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        title_rows = [row for row in usage_rows if row["type"] == "session_title"]
        assert title_rows and title_rows[0]["model"] == "gpt-6-luna" and title_rows[0]["input_tokens"] == 80

        # A second turn does not ask again.
        await client.post("/api/chat", json={"message": "and then?", "session_id": "s1", "metadata": {"runtime_profile": COPILOT_PROFILE}})
        await _settle_background(app)
        assert len(title_proxy.requests) == 1
    finally:
        await client.close()


async def test_rename_wins_and_regenerate_respects_it(tmp_path, monkeypatch, title_proxy):
    app, client = await _app_client(tmp_path, monkeypatch, title_proxy.base_url)
    try:
        await client.post("/api/chat", json={"message": USER_TEXT, "session_id": "s1", "metadata": {"runtime_profile": COPILOT_PROFILE}})
        await _settle_background(app)

        renamed = await (await client.post("/api/sessions/s1/rename", json={"name": "My notes"})).json()
        assert renamed == {"success": True, "session_id": "s1", "name": "My notes", "name_source": "custom"}
        assert (await (await client.get("/api/sessions/s1/name")).json())["name_source"] == "custom"

        regenerated = await (await client.post("/api/sessions/s1/title/generate", json={"force": True})).json()
        assert regenerated == {"success": True, "generated": False, "session_id": "s1", "name": "My notes", "name_source": "custom"}
        assert len(title_proxy.requests) == 1  # the model was not asked again
    finally:
        await client.close()


async def test_generate_endpoint_regenerates_only_when_forced(tmp_path, monkeypatch, title_proxy):
    app, client = await _app_client(tmp_path, monkeypatch, title_proxy.base_url)
    overlay = app[SETTINGS_KEY].adapter_state_dir / "runtime-profile-overlay.json"
    overlay.write_text(json.dumps({"config": {"llm": {"provider": "github_copilot"}}}), encoding="utf-8")
    try:
        await client.post("/api/chat", json={"message": USER_TEXT, "session_id": "s1", "metadata": {"runtime_profile": COPILOT_PROFILE}})
        await _settle_background(app)
        assert len(title_proxy.requests) == 1

        kept = await (await client.post("/api/sessions/s1/title/generate")).json()
        assert kept["generated"] is False and kept["name_source"] == "generated"
        assert len(title_proxy.requests) == 1

        title_proxy.reply = {"choices": [{"message": {"content": "Deploy stage permissions"}}]}
        forced = await (await client.post("/api/sessions/s1/title/generate", json={"force": True})).json()
        assert forced == {"success": True, "generated": True, "session_id": "s1", "name": "Deploy stage permissions", "name_source": "generated"}
        assert len(title_proxy.requests) == 2
    finally:
        await client.close()


async def test_generate_endpoint_error_statuses(tmp_path, monkeypatch, title_proxy):
    class _OnlyUserClient(FakeOpenCodeClient):
        async def send_message(self, session_id, *, parts, model, agent, system=None, message_id=None, no_reply=None, tools=None):
            user = {"id": message_id or "u-only", "role": "user", "parts": parts}
            self.messages[session_id].append(user)
            return {"message": user}

    app, client = await _app_client(tmp_path, monkeypatch, title_proxy.base_url, opencode_client=_OnlyUserClient())
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

        unanswered = await client.post("/api/sessions/s-user-only/title/generate", json={"force": True})
        assert unanswered.status == 422
        assert "no completed turn" in (await unanswered.json())["error"]

        fake.messages[created["id"]].append({"id": "a1", "role": "assistant", "parts": [{"type": "text", "text": "hi"}]})
        title_proxy.status = 500
        title_proxy.reply = {"error": "boom"}
        failed = await client.post("/api/sessions/s-user-only/title/generate", json={"force": True})
        assert failed.status == 502

        async def timed_out(*_args, **_kwargs):
            raise TitleGenerationError("Title generation timed out after 10s.", status_code=504)

        monkeypatch.setattr(session_title, "request_title", timed_out)
        slow = await client.post("/api/sessions/s-user-only/title/generate", json={"force": True})
        assert slow.status == 504
        assert (await (await client.get("/api/sessions/s-user-only/name")).json())["name_source"] == "derived"
    finally:
        await client.close()


async def test_generate_endpoint_without_a_provider_is_a_502(tmp_path, monkeypatch, title_proxy):
    app, client = await _app_client(tmp_path, monkeypatch, title_proxy.base_url)
    try:
        await client.post("/api/chat", json={"message": USER_TEXT, "session_id": "s1"})
        await _settle_background(app)
        assert title_proxy.requests == []  # no runtime profile and no overlay: nothing to call
        resp = await client.post("/api/sessions/s1/title/generate", json={"force": True})
        assert resp.status == 502
        assert "No model proxy" in (await resp.json())["error"]
    finally:
        await client.close()


async def test_a_rename_during_generation_wins(tmp_path, monkeypatch):
    slow = _TitleProxy(delay=0.3)
    base_url = await slow.start()
    app, client = await _app_client(tmp_path, monkeypatch, base_url)
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


async def test_background_failure_leaves_the_derived_name(tmp_path, monkeypatch, caplog):
    failing = _TitleProxy(reply={"error": "boom"}, status=500)
    base_url = await failing.start()
    app, client = await _app_client(tmp_path, monkeypatch, base_url)
    try:
        chat = await client.post("/api/chat", json={"message": USER_TEXT, "session_id": "s1", "metadata": {"runtime_profile": COPILOT_PROFILE}})
        assert chat.status == 200
        await _settle_background(app)
        name = await (await client.get("/api/sessions/s1/name")).json()
        assert name["name_source"] == "derived"
        assert "s1" not in session_title._inflight
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


async def test_publish_session_title_is_quiet_about_failures(monkeypatch):
    portal = _Portal(status=404)
    base = await portal.start()
    try:
        monkeypatch.setenv("PORTAL_INTERNAL_BASE_URL", base)
        monkeypatch.setenv("PORTAL_AGENT_ID", "agent")
        out = await PortalMetadataClient(Settings.from_env()).publish_session_title(session_id="s1", title="x")
        assert out["success"] is False and out["status"] == 404
    finally:
        await portal.close()
    monkeypatch.delenv("PORTAL_INTERNAL_BASE_URL", raising=False)
    monkeypatch.delenv("PORTAL_AGENT_ID", raising=False)
    assert await PortalMetadataClient(Settings.from_env()).publish_session_title(session_id="s1", title="x") == {"success": False, "skipped": True}
    monkeypatch.setenv("PORTAL_INTERNAL_BASE_URL", "http://portal")
    monkeypatch.setenv("PORTAL_AGENT_ID", "agent")
    assert await PortalMetadataClient(Settings.from_env()).publish_session_title(session_id="s1", title="   ") == {"success": False, "skipped": True}


async def test_first_turn_publishes_the_title_to_the_portal(tmp_path, monkeypatch, title_proxy):
    portal = _Portal()
    base = await portal.start()
    monkeypatch.setenv("PORTAL_INTERNAL_BASE_URL", base)
    monkeypatch.setenv("PORTAL_AGENT_ID", "agent-1")
    app, client = await _app_client(tmp_path, monkeypatch, title_proxy.base_url)
    try:
        await client.post("/api/chat", json={"message": USER_TEXT, "session_id": "s1", "metadata": {"runtime_profile": COPILOT_PROFILE}})
        await _settle_background(app)
        patches = [call for call in portal.calls if call["method"] == "PATCH"]
        assert patches == [
            {"method": "PATCH", "raw_path": "/api/internal/agents/agent-1/sessions/s1/title", "body": {"title": "Release pipeline permissions"}, "auth": None}
        ]
        # The next full publish carries the name too.
        await client.post("/api/chat", json={"message": "more", "session_id": "s1", "metadata": {"runtime_profile": COPILOT_PROFILE}})
        puts = [call for call in portal.calls if call["method"] == "PUT"]
        assert json.loads(puts[-1]["body"]["metadata_json"])["session_title"] == "Release pipeline permissions"
    finally:
        await client.close()
        await portal.close()


def test_usage_tracker_records_title_calls(tmp_path):
    tracker = UsageTracker(tmp_path / "usage.jsonl")
    row = tracker.record_session_title(session_id="s1", model="gpt-6-luna", provider="github-copilot", usage={"prompt_tokens": 80, "completion_tokens": 6})
    assert row["type"] == "session_title" and row["input_tokens"] == 80 and row["output_tokens"] == 6 and row["messages"] == 0
    summary = tracker.summarize(days=1)
    assert summary["global"]["total_requests"] == 1
    assert summary["by_model"][0]["model"] == "gpt-6-luna"
