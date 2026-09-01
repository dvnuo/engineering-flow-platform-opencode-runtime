"""Assistant personalization loaded from the agents repository.

Portal's init container clones the agents repo into the workspace at boot and
copies its optional ``portal/`` directory alongside the ``AGENTS.md`` and
``instructions/`` this runtime already consumes. Serving the greeting and the
starter cards from here -- rather than having Portal clone the repo a second
time -- keeps one source of truth and guarantees what a member sees matches the
branch the assistant actually booted with.

Expected layout at the workspace root:

    portal/welcome.md    greeting shown when a chat has no messages yet
    portal/cards.yaml    starter prompts offered as clickable cards

Both files are optional. A branch without them simply has no personalization,
and Portal falls back to its generic welcome.

The native runtime carries a sibling of this module. It parses with ruamel.yaml
because that is what *it* declares; this repository declares PyYAML, so the
import here is deliberately different. Copying either file across repositories
verbatim would import a library its own image never installs.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

PORTAL_PERSONALIZATION_DIRNAME = "portal"
WELCOME_FILENAME = "welcome.md"
CARDS_FILENAME = "cards.yaml"

# A greeting is prose, not a document, and cards are a short menu. Both are
# capped so a mistaken commit in the agents repo degrades the panel instead of
# flooding the chat pane. The limits match the native runtime so a behavior
# pack branch renders the same on either engine.
MAX_WELCOME_CHARS = 4000
MAX_CARDS = 12
MAX_CARD_FIELD_CHARS = 500
MAX_PROMPT_CHARS = 4000


def load_personalization(workspace_root: Path) -> dict[str, Any]:
    """Return {"welcome": str|None, "cards": list} for one workspace."""

    portal_dir = Path(workspace_root) / PORTAL_PERSONALIZATION_DIRNAME
    return {
        "welcome": _load_welcome(portal_dir / WELCOME_FILENAME),
        "cards": _load_cards(portal_dir / CARDS_FILENAME),
    }


def _load_welcome(path: Path) -> str | None:
    text = _read_text(path)
    if not text:
        return None
    return text[:MAX_WELCOME_CHARS]


def _load_cards(path: Path) -> list[dict[str, Any]]:
    text = _read_text(path)
    if not text:
        return []
    try:
        parsed = yaml.safe_load(text)
    except Exception:
        # A bad commit in the agents repo must degrade the panel, never break
        # chat. `yaml` is imported at module scope, so a missing dependency
        # fails loudly at import rather than being mistaken for bad YAML here.
        logger.warning("Ignoring unreadable personalization cards at %s", path, exc_info=True)
        return []

    raw_cards = parsed.get("cards") if isinstance(parsed, Mapping) else parsed
    if not isinstance(raw_cards, list):
        return []

    cards: list[dict[str, Any]] = []
    for raw in raw_cards[:MAX_CARDS]:
        card = _normalize_card(raw)
        if card is not None:
            cards.append(card)
    return cards


def _normalize_card(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    title = _clean(raw.get("title"), MAX_CARD_FIELD_CHARS)
    prompt = _clean(raw.get("prompt"), MAX_PROMPT_CHARS)
    # A card with no title has nothing to click; one with no prompt does
    # nothing when clicked. Either way it is not worth rendering.
    if not title or not prompt:
        return None

    card: dict[str, Any] = {
        "title": title,
        "description": _clean(raw.get("description"), MAX_CARD_FIELD_CHARS),
        "icon": _clean(raw.get("icon"), 64) or "sparkles",
        "prompt": prompt,
    }

    raw_input = raw.get("input")
    if isinstance(raw_input, Mapping):
        card["input"] = {
            "label": _clean(raw_input.get("label"), MAX_CARD_FIELD_CHARS) or "Details",
            "placeholder": _clean(raw_input.get("placeholder"), MAX_CARD_FIELD_CHARS),
        }
    return card


def _clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _read_text(path: Path) -> str:
    try:
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        logger.debug("Could not read personalization file %s", path, exc_info=True)
        return ""
