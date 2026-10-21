"""Generated session titles: one small model call after the first answered turn.

The same contract as the native runtime (src/gateway/session_title.py there),
so the Portal does not care which runtime answered: the same instructions,
prompt shape and cleaning rules; the same name precedence (a manual rename,
then the generated title, then the title the session was created with); the
same endpoints (GET /api/sessions/{id}/name, POST
/api/sessions/{id}/title/generate); and the same best-effort PATCH that tells
the Portal the name.

The model call goes through the adapter's own loopback proxies
(/api/internal/copilot, /api/internal/ai-platform), which already carry the
member's credentials and outbound proxy settings, with a cheap model.
Everything here is best effort: a timeout or a provider error leaves the
session as it was, and nothing blocks or fails the chat turn that triggered it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Mapping

import aiohttp
from aiohttp import web

from .app_keys import (
    OPENCODE_CLIENT_KEY,
    PORTAL_METADATA_CLIENT_KEY,
    SESSION_STORE_KEY,
    SETTINGS_KEY,
    TASK_BACKGROUND_TASKS_KEY,
    USAGE_TRACKER_KEY,
    USER_DISPLAY_STORE_KEY,
)
from .opencode_client import OpenCodeClientError
from .opencode_config import normalize_opencode_provider_id
from .opencode_message_adapter import message_id as adapter_message_id
from .opencode_message_adapter import message_role as adapter_message_role
from .opencode_message_adapter import message_to_visible_text
from .profile_store import ProfileOverlayStore
from .session_store import SessionRecord, session_id_is_task_session
from .settings import Settings
from .thinking_events import safe_preview

logger = logging.getLogger(__name__)

# The prompt asks for 40 characters; the hard cut after cleaning leaves room
# for a model that counts differently.
TITLE_MAX_CHARS = 60
# Of each side of the first turn. A title needs the gist, not the transcript.
TITLE_INPUT_MAX_CHARS = 1500
TITLE_TIMEOUT_SECONDS = 10.0
TITLE_TASK_TYPE = "session_title"

# The cheap model per provider family, keyed by the normalized OpenCode
# provider id. ``llm.title_model`` in the runtime profile overrides both.
TITLE_MODELS = {
    "github-copilot": "gpt-6-luna",
    "ai-platform": "gpt-5.6-luna",
}

TITLE_INSTRUCTIONS = (
    "You name chat sessions for a list of past conversations. "
    "Given the first message of a conversation and the assistant's answer, reply with a short title.\n"
    "Rules:\n"
    "- A noun phrase of at most 40 characters (about 20 characters for Chinese, Japanese or Korean).\n"
    "- The same language as the user's message.\n"
    "- Name the topic or task, not the fact that it was asked or answered.\n"
    "- No quotes, no trailing period, no prefix such as 'Title:' or 'Session about'.\n"
    "Reply with the title only."
)

_TITLE_PREFIX = re.compile(r"^(?:title|session title|session name|name|标题)\s*[:：]\s*", re.IGNORECASE)
_QUOTES = "\"'`“”‘’「」『』《》"
_TRAILING_PUNCTUATION = ".。!！"

_inflight: set[str] = set()


class TitleGenerationError(RuntimeError):
    """The title could not be generated; ``status_code`` is what an endpoint reports."""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class TitleTarget:
    """Which loopback proxy and model answer the title call."""

    provider: str
    model: str
    base_url: str

    @property
    def url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"


def clean_title(raw: Any) -> str:
    """The model's reply as a name: first line, no quotes, no prefix, no period, bounded."""

    text = ""
    for line in str(raw or "").splitlines():
        candidate = line.strip()
        if candidate:
            text = candidate
            break
    if not text:
        return ""
    text = _TITLE_PREFIX.sub("", text).strip()
    text = text.strip(_QUOTES).strip()
    text = re.sub(r"\s+", " ", text)
    text = text.rstrip(_TRAILING_PUNCTUATION).strip()
    if len(text) > TITLE_MAX_CHARS:
        text = text[: TITLE_MAX_CHARS - 1].rstrip() + "…"
    return text


def _excerpt(text: str) -> str:
    collapsed = re.sub(r"[ \t]+", " ", str(text or "")).strip()
    if len(collapsed) <= TITLE_INPUT_MAX_CHARS:
        return collapsed
    return collapsed[:TITLE_INPUT_MAX_CHARS].rstrip() + "…"


def build_title_messages(user_text: str, assistant_text: str) -> list[dict[str, str]]:
    """The one-shot chat/completions messages: instructions plus the first turn's excerpts."""

    prompt = (
        "User message:\n"
        f"{_excerpt(user_text)}\n\n"
        "Assistant answer:\n"
        f"{_excerpt(assistant_text)}\n\n"
        "Title:"
    )
    return [
        {"role": "system", "content": TITLE_INSTRUCTIONS},
        {"role": "user", "content": prompt},
    ]


def _llm_config(runtime_profile: Mapping[str, Any] | None) -> dict[str, Any]:
    """The ``llm`` section a chat request carried (profile config, with top-level provider/model as fallbacks)."""

    if not isinstance(runtime_profile, Mapping):
        return {}
    config = runtime_profile.get("config") if isinstance(runtime_profile.get("config"), Mapping) else {}
    llm = dict(config.get("llm")) if isinstance(config.get("llm"), Mapping) else {}
    for key in ("provider", "model", "title_model"):
        value = runtime_profile.get(key)
        if isinstance(value, str) and value.strip() and not llm.get(key):
            llm[key] = value
    return llm


def _llm_config_from_overlay(settings: Settings) -> dict[str, Any]:
    """The pod's own profile projection, for calls that carry no runtime profile (the endpoint)."""

    try:
        overlay = ProfileOverlayStore(settings).load()
    except Exception:  # noqa: BLE001 - a broken overlay only means no title
        return {}
    if overlay is None or not isinstance(overlay.config, Mapping):
        return {}
    llm = overlay.config.get("llm")
    return dict(llm) if isinstance(llm, Mapping) else {}


def _provider_id(llm: Mapping[str, Any]) -> str:
    provider = llm.get("provider")
    if isinstance(provider, str) and provider.strip():
        return normalize_opencode_provider_id(provider)
    model = llm.get("model")
    if isinstance(model, str) and "/" in model:
        return normalize_opencode_provider_id(model.split("/", 1)[0])
    return ""


def resolve_title_target(settings: Settings, runtime_profile: Mapping[str, Any] | None = None) -> TitleTarget | None:
    """The proxy and model for a title call, or None when the configured provider has no loopback proxy."""

    llm = _llm_config(runtime_profile)
    if not _provider_id(llm):
        llm = {**_llm_config_from_overlay(settings), **{k: v for k, v in llm.items() if v}}
    provider = _provider_id(llm)
    if provider not in TITLE_MODELS:
        return None
    base_url = settings.copilot_proxy_base_url if provider == "github-copilot" else settings.ai_platform_proxy_base_url
    base_url = str(base_url or "").strip().rstrip("/")
    if not base_url:
        return None
    override = llm.get("title_model")
    model = override.strip() if isinstance(override, str) and override.strip() else TITLE_MODELS[provider]
    if "/" in model:
        model = model.split("/", 1)[1]
    return TitleTarget(provider=provider, model=model, base_url=base_url)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for item in content:
            if isinstance(item, Mapping):
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
            elif isinstance(item, str):
                chunks.append(item)
        return "".join(chunks)
    return ""


def extract_completion_text(payload: Any) -> str:
    """The assistant text of a chat/completions reply (choices[0].message.content), or ""."""

    if not isinstance(payload, Mapping):
        return ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], Mapping) else {}
        message = first.get("message") if isinstance(first.get("message"), Mapping) else {}
        text = _content_text(message.get("content"))
        if text:
            return text
        text = _content_text(first.get("text"))
        if text:
            return text
    output_text = payload.get("output_text")
    if isinstance(output_text, str):
        return output_text
    return _content_text(payload.get("content"))


async def request_title(
    target: TitleTarget,
    *,
    user_text: str,
    assistant_text: str,
    timeout: float = TITLE_TIMEOUT_SECONDS,
) -> tuple[str, dict[str, Any]]:
    """One non-streaming chat/completions call through the loopback proxy: (text, usage)."""

    payload = {
        "model": target.model,
        "messages": build_title_messages(user_text, assistant_text),
        "stream": False,
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(target.url, json=payload) as response:
                body = await response.read()
                if response.status >= 400:
                    preview = safe_preview(body.decode("utf-8", errors="replace"), 300)
                    raise TitleGenerationError(f"Title provider answered {response.status}: {preview}")
                try:
                    data = await response.json(content_type=None)
                except Exception as exc:  # noqa: BLE001 - a non-JSON body is a provider failure
                    raise TitleGenerationError("Title provider returned a non-JSON reply.") from exc
    except TitleGenerationError:
        raise
    except asyncio.TimeoutError as exc:
        raise TitleGenerationError(f"Title generation timed out after {timeout:g}s.", status_code=504) from exc
    except aiohttp.ClientError as exc:
        raise TitleGenerationError(f"Title generation failed: {exc}") from exc
    usage = data.get("usage") if isinstance(data, Mapping) and isinstance(data.get("usage"), Mapping) else {}
    return extract_completion_text(data), dict(usage)


def first_turn_texts(
    messages: list[Any],
    *,
    display_store: Any = None,
    portal_session_id: str = "",
    opencode_session_id: str = "",
) -> tuple[str, str]:
    """The first user message (the member's own words when recorded) and the first assistant text after it."""

    user_text = ""
    for message in messages or []:
        role = adapter_message_role(message)
        if not user_text:
            if role == "user":
                user_text = _user_display_text(
                    message,
                    display_store=display_store,
                    portal_session_id=portal_session_id,
                    opencode_session_id=opencode_session_id,
                )
            continue
        if role == "assistant":
            assistant_text = message_to_visible_text(message).strip()
            if assistant_text:
                return user_text, assistant_text
    return user_text, ""


def _user_display_text(message: Any, *, display_store: Any, portal_session_id: str, opencode_session_id: str) -> str:
    # A turn whose model prompt was expanded (attachment context ahead of the
    # question) keeps the member's own words in the display store; the title
    # should come from those, not from the composed prompt.
    if display_store is not None:
        try:
            display = display_store.get_user_message(
                opencode_session_id=opencode_session_id,
                opencode_message_id=adapter_message_id(message),
                portal_session_id=portal_session_id,
            )
        except Exception:  # noqa: BLE001 - the display store is a convenience
            display = None
        if isinstance(display, Mapping):
            visible = display.get("display_content")
            if isinstance(visible, str) and visible.strip():
                return visible.strip()
    return message_to_visible_text(message).strip()


def _record_usage(app: web.Application, *, session_id: str, target: TitleTarget, usage: Mapping[str, Any]) -> None:
    tracker = app.get(USAGE_TRACKER_KEY)
    record = getattr(tracker, "record_session_title", None)
    if not callable(record) or not usage:
        return
    try:
        record(session_id=session_id, model=target.model, provider=target.provider, usage=dict(usage))
    except Exception:  # noqa: BLE001 - accounting never fails a title
        logger.debug("session title usage was not recorded", exc_info=True)


async def generate_session_title(
    app: web.Application,
    portal_session_id: str,
    *,
    runtime_profile: Mapping[str, Any] | None = None,
    force: bool = False,
    timeout: float = TITLE_TIMEOUT_SECONDS,
) -> str | None:
    """Generate and store a title for the session; the stored name, or None when nothing changed.

    None means the session keeps the name it has: a manual name exists, or a
    generated one does and ``force`` is false. An unknown or deleted session
    raises ``KeyError``; no answered turn, no usable provider, a provider
    failure and a timeout raise ``TitleGenerationError``.
    """

    store = app[SESSION_STORE_KEY]
    record = store.get(portal_session_id)
    if record is None or record.deleted:
        raise KeyError(portal_session_id)
    if record.custom_title:
        return None
    if record.generated_title and not force:
        return None

    target = resolve_title_target(app[SETTINGS_KEY], runtime_profile)
    if target is None:
        raise TitleGenerationError("No model proxy is configured for session titles.")

    client = app[OPENCODE_CLIENT_KEY]
    try:
        messages = await client.list_messages(record.opencode_session_id)
    except OpenCodeClientError as exc:
        raise TitleGenerationError(f"Could not read the session from OpenCode: {exc}") from exc
    user_text, assistant_text = first_turn_texts(
        messages,
        display_store=app.get(USER_DISPLAY_STORE_KEY),
        portal_session_id=portal_session_id,
        opencode_session_id=record.opencode_session_id,
    )
    if not user_text or not assistant_text:
        raise TitleGenerationError("The session has no completed turn to name yet.", status_code=422)

    text, usage = await request_title(target, user_text=user_text, assistant_text=assistant_text, timeout=timeout)
    title = clean_title(text)
    if not title:
        raise TitleGenerationError("The model returned no usable title.")

    # A rename that landed while the model was thinking wins.
    latest = store.get(portal_session_id)
    if latest is None or latest.deleted:
        raise KeyError(portal_session_id)
    if latest.custom_title:
        return None
    store.set_generated_title(portal_session_id, title)
    _record_usage(app, session_id=portal_session_id, target=target, usage=usage)
    return title


def should_generate_after_turn(record_before: SessionRecord | None, *, completion_state: str, assistant_text: str) -> bool:
    """Only the first completed chat turn with an answer, in a session not yet named."""

    if record_before is None or record_before.deleted:
        return False
    if session_id_is_task_session(record_before.portal_session_id):
        return False
    if str(completion_state or "").strip().lower() != "completed":
        return False
    if not str(assistant_text or "").strip():
        return False
    if record_before.message_count > 0:
        return False
    if record_before.custom_title or record_before.generated_title:
        return False
    return True


def schedule_session_title(
    app: web.Application,
    portal_session_id: str,
    *,
    runtime_profile: Mapping[str, Any] | None = None,
) -> asyncio.Task | None:
    """Start the title call in the background; the task, or None when there is nothing to call."""

    if portal_session_id in _inflight:
        return None
    if resolve_title_target(app[SETTINGS_KEY], runtime_profile) is None:
        return None
    _inflight.add(portal_session_id)
    task = asyncio.create_task(
        _generate_in_background(app, portal_session_id, runtime_profile=runtime_profile),
        name=f"session-title:{portal_session_id}",
    )
    task_set = app.get(TASK_BACKGROUND_TASKS_KEY)
    if isinstance(task_set, set):
        task_set.add(task)
        task.add_done_callback(task_set.discard)
    return task


async def _generate_in_background(
    app: web.Application,
    portal_session_id: str,
    *,
    runtime_profile: Mapping[str, Any] | None,
) -> None:
    try:
        title = await generate_session_title(app, portal_session_id, runtime_profile=runtime_profile)
        if title:
            logger.info("Session %s titled: %s", portal_session_id, title)
            await publish_title_to_portal(app, portal_session_id, title)
    except TitleGenerationError as exc:
        logger.warning("Session %s keeps its derived name: %s", portal_session_id, exc.message)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - best effort, never a crash
        logger.warning("Session %s keeps its derived name (title generation failed)", portal_session_id, exc_info=True)
    finally:
        _inflight.discard(portal_session_id)


async def publish_title_to_portal(app: web.Application, portal_session_id: str, title: str) -> None:
    client = app.get(PORTAL_METADATA_CLIENT_KEY)
    publish = getattr(client, "publish_session_title", None)
    if not callable(publish):
        return
    try:
        await publish(session_id=portal_session_id, title=title)
    except Exception:  # noqa: BLE001 - the Portal learns the name on the next publish anyway
        logger.debug("session title publish failed", exc_info=True)


__all__ = [
    "TITLE_INSTRUCTIONS",
    "TITLE_MODELS",
    "TitleGenerationError",
    "TitleTarget",
    "build_title_messages",
    "clean_title",
    "extract_completion_text",
    "first_turn_texts",
    "generate_session_title",
    "publish_title_to_portal",
    "request_title",
    "resolve_title_target",
    "schedule_session_title",
    "should_generate_after_turn",
]
