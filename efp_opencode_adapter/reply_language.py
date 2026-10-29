"""The member's reply language: a Portal preference rendered as one system-prompt block.

The Portal resolves each member's language (an explicit "Assistant replies
in" choice, else the UI language or the browser's) and sends it with every
proxied request as ``metadata.portal_user.reply_language`` plus
``reply_language_strict`` (true when the member chose it). The texts live in
``prompts/reply_language.json``, JSON-escaped so the repository stays ASCII
and shared verbatim with the native runtime, so wording changes never touch
the code here.

This adapter does not build the system prompt: OpenCode does, and appends
the request's ``system`` string at the very end of it for every step of the
turn. The block therefore leads that string (``prepend_reply_language_block``)
and is never a message part, since parts feed the Copilot auto-routing prompt
and the first-message conversation key.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

REPLY_LANGUAGE_TEXTS_PATH = Path(__file__).resolve().parent / "prompts" / "reply_language.json"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReplyLanguage:
    """A reply language that changes the prompt.

    ``tag`` is one of the tags in the texts file; ``strict`` is whether the
    member chose the language explicitly (always reply in it) rather than the
    Portal resolving it from the UI language (follow the member's message).
    """

    tag: str
    strict: bool

    @property
    def display_name(self) -> str:
        return str(_texts()["languages"][self.tag]["display_name"])


def resolve_reply_language(metadata: Mapping[str, Any] | None) -> ReplyLanguage | None:
    """The reply language a request's metadata selects, or None for today's prompt.

    None covers everything that must not change the prompt: no ``portal_user``
    object, a missing or unknown tag, and English without an explicit choice,
    which is what every member got before the setting existed. Only a JSON
    ``true`` makes the choice strict.
    """
    if not isinstance(metadata, Mapping):
        return None
    portal_user = metadata.get("portal_user")
    if not isinstance(portal_user, Mapping):
        return None
    tag = portal_user.get("reply_language")
    if not isinstance(tag, str):
        return None
    tag = tag.strip()
    if tag not in _texts()["languages"]:
        return None
    strict = portal_user.get("reply_language_strict") is True
    if tag == "en" and not strict:
        return None
    return ReplyLanguage(tag=tag, strict=strict)


def render_reply_language_block(reply_language: ReplyLanguage) -> str:
    """The ``Reply language:`` system block for ``reply_language``."""
    texts = _texts()
    language = texts["languages"][reply_language.tag]
    display_name = str(language["display_name"])
    mode_sentence = str(texts["mode_sentences"]["strict" if reply_language.strict else "soft"])
    return str(texts["block"]).format(
        display_name=display_name,
        language_sentence=str(language["language_sentence"]),
        mode_sentence=mode_sentence.format(display_name=display_name),
    )


def prepend_reply_language_block(system: str | None, reply_language: ReplyLanguage | None) -> str | None:
    """``system`` led by the block, or ``system`` itself, unchanged, when no block applies.

    The block comes first and the caller's text keeps today's composition
    after a blank line; a blank ``system`` becomes the block alone.
    """
    if reply_language is None:
        return system
    block = render_reply_language_block(reply_language)
    if isinstance(system, str) and system.strip():
        return f"{block}\n\n{system}"
    return block


@lru_cache(maxsize=1)
def _texts() -> dict[str, Any]:
    # A missing or unreadable texts file must not fail the run: with no
    # languages known, no tag resolves and every run keeps today's prompt.
    try:
        with REPLY_LANGUAGE_TEXTS_PATH.open(encoding="utf-8") as handle:
            texts = json.load(handle)
        if not isinstance(texts, dict) or not isinstance(texts.get("languages"), dict):
            raise ValueError("no languages table")
        return texts
    except (OSError, ValueError) as exc:
        logger.warning("reply language texts unavailable at %s; no Reply language block: %s", REPLY_LANGUAGE_TEXTS_PATH, exc)
        return {"languages": {}}


__all__ = [
    "REPLY_LANGUAGE_TEXTS_PATH",
    "ReplyLanguage",
    "prepend_reply_language_block",
    "render_reply_language_block",
    "resolve_reply_language",
]
