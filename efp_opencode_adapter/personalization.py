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

A card is clicked to send a prompt. It can describe that prompt in two ways:

    prompt + input       the legacy form: one free-text value replaces
                         {{input}} in the prompt
    fields + template    the form introduced with card contract version 2:
                         up to six named fields, each replacing its own
                         {{name}} placeholder in the template

A card may carry both, so one cards.yaml serves a Portal of either age. An
`input` or a text field can also name a `source` (with optional `params`):
the id of a picker Portal knows how to offer instead of a bare text box. The
runtime only validates the shape of those keys; what a source means is
Portal's business.

The native runtime carries a sibling of this module. It parses with ruamel.yaml
because that is what *it* declares; this repository declares PyYAML, so the
import here is deliberately different. Copying either file across repositories
verbatim would import a library its own image never installs.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from itertools import islice
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

# Card contract version 2. Every limit below is shared with the native runtime
# for the same reason as the ones above. Sizes are capped by cutting (the
# first N entries, the first N characters), the way cards and text already
# are; only the rules that would change what a card means when cut -- a
# seventh field, an over-long source id -- reject instead.
CARD_FORM_VERSION = 2
MAX_SOURCE_CHARS = 64
MAX_PARAMS = 8
MAX_PARAM_CHARS = 200
MAX_PARAM_LIST_ITEMS = 10
MAX_PARAM_LIST_ITEM_CHARS = 100
MAX_FIELDS = 6
MAX_FIELD_OPTIONS = 20
MAX_FIELD_OPTION_CHARS = 200
MAX_FIELD_DEFAULTS = 20
MAX_FIELD_ACCEPT = 8
FIELD_TYPES = ("text", "textarea", "choice", "file")

# These three are applied with fullmatch, so they carry no anchors.
_SOURCE_RE = re.compile(r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)*")
# Field names and params keys share one shape.
_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
_ACCEPT_RE = re.compile(r"\.[A-Za-z0-9]{1,10}")
# What Portal substitutes in a template. Anything else in braces is text.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")


def load_personalization(workspace_root: Path, *, version: int = 1) -> dict[str, Any]:
    """Return {"welcome": str|None, "cards": list} for one workspace.

    `version` is the card contract the caller understands. Below 2 every card
    comes back in the legacy shape and a card that only has the form is left
    out, so a Portal that predates the form never receives a card it cannot
    send. From 2 on, a card also carries its `fields` and `template`.
    """

    portal_dir = Path(workspace_root) / PORTAL_PERSONALIZATION_DIRNAME
    return {
        "welcome": _load_welcome(portal_dir / WELCOME_FILENAME),
        "cards": _load_cards(portal_dir / CARDS_FILENAME, version),
    }


def _load_welcome(path: Path) -> str | None:
    text = _read_text(path)
    if not text:
        return None
    return text[:MAX_WELCOME_CHARS]


def _load_cards(path: Path, version: int = 1) -> list[dict[str, Any]]:
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
        card = _normalize_card(raw, version)
        if card is not None:
            cards.append(card)
    return cards


def _normalize_card(raw: Any, version: int = 1) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    title = _clean(raw.get("title"), MAX_CARD_FIELD_CHARS)
    prompt = _clean(raw.get("prompt"), MAX_PROMPT_CHARS)
    # The form is only read for a caller that asked for it. A legacy caller
    # can neither render the fields nor fill the template, so to that caller
    # a form-only card is simply a card with no prompt.
    form = _normalize_form(raw) if version >= CARD_FORM_VERSION else None
    # A card with no title has nothing to click; one with no prompt in either
    # form does nothing when clicked. Either way it is not worth rendering.
    if not title or not (prompt or form):
        return None

    card: dict[str, Any] = {
        "title": title,
        "description": _clean(raw.get("description"), MAX_CARD_FIELD_CHARS),
        "icon": _clean(raw.get("icon"), 64) or "sparkles",
        "prompt": prompt,
    }

    raw_input = raw.get("input")
    # `input` only exists to fill {{input}} in the legacy prompt, so a card
    # without that prompt has nothing for it to fill.
    if prompt and isinstance(raw_input, Mapping):
        card["input"] = _normalize_input(raw_input)
    if form is not None:
        card["fields"], card["template"] = form
    return card


def _normalize_input(raw_input: Mapping[Any, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {
        "label": _clean(raw_input.get("label"), MAX_CARD_FIELD_CHARS) or "Details",
        "placeholder": _clean(raw_input.get("placeholder"), MAX_CARD_FIELD_CHARS),
    }
    normalized.update(_source_keys(raw_input))
    # Only the YAML boolean counts. Any other truthy value ("yes" in quotes,
    # a number) is more likely a slip than a request for a text area.
    if raw_input.get("multiline") is True:
        normalized["multiline"] = True
    return normalized


def _source_keys(raw: Mapping[Any, Any]) -> dict[str, Any]:
    """The valid `source` / `params` of an input or a text field.

    Each key is left out when it is not usable, so an older Portal -- or a
    newer one handed a typo -- still shows a plain text box.
    """
    keys: dict[str, Any] = {}
    source = raw.get("source")
    if isinstance(source, str):
        source = source.strip()
        # An id is matched whole or not at all: cutting an over-long one
        # would name a different source.
        if len(source) <= MAX_SOURCE_CHARS and _SOURCE_RE.fullmatch(source):
            keys["source"] = source
    params = _normalize_params(raw.get("params"))
    if params:
        keys["params"] = params
    return keys


def _normalize_params(raw: Any) -> dict[str, Any]:
    """Arguments for a source: a small, flat, JSON-safe mapping.

    Portal forwards these to whatever backs the source, so nothing nested and
    nothing of unbounded size is let through. Bad entries are dropped one by
    one rather than voiding their neighbours.
    """
    if not isinstance(raw, Mapping):
        return {}
    params: dict[str, Any] = {}
    for key, value in islice(raw.items(), MAX_PARAMS):
        # PyYAML hands back non-string keys for `1:` or `yes:`.
        if not isinstance(key, str) or not _NAME_RE.fullmatch(key):
            continue
        # A value of any other kind (a float, a nested mapping, null) is
        # dropped with its key.
        if isinstance(value, (bool, int)):
            params[key] = value
        elif isinstance(value, str):
            params[key] = _clean(value, MAX_PARAM_CHARS)
        elif isinstance(value, list):
            items = (_clean(item, MAX_PARAM_LIST_ITEM_CHARS) for item in value[:MAX_PARAM_LIST_ITEMS])
            params[key] = [item for item in items if item]
    return params


def _normalize_form(raw: Mapping[Any, Any]) -> tuple[list[dict[str, Any]], str] | None:
    """The card's `fields` and `template`, or None when they are not usable.

    The two are one unit: fields nobody substitutes, or a template naming a
    field that was never declared, would send a prompt with holes in it. So a
    flaw in either drops both, and the card falls back to its legacy prompt
    when it has one.
    """
    raw_fields = raw.get("fields")
    template = _clean(raw.get("template"), MAX_PROMPT_CHARS)
    if not template or not isinstance(raw_fields, list):
        return None
    if not 1 <= len(raw_fields) <= MAX_FIELDS:
        return None

    fields: list[dict[str, Any]] = []
    names: set[str] = set()
    for raw_field in raw_fields:
        field = _normalize_field(raw_field)
        if field is None or field["name"] in names:
            return None
        names.add(field["name"])
        fields.append(field)

    # The reverse is fine: a field no placeholder uses is still collected
    # (a file field is attached, never substituted).
    if any(name not in names for name in _PLACEHOLDER_RE.findall(template)):
        return None
    return fields, template


def _normalize_field(raw: Any) -> dict[str, Any] | None:
    """One form field, or None when it invalidates the whole form."""
    if not isinstance(raw, Mapping):
        return None
    name = raw.get("name")
    name = name.strip() if isinstance(name, str) else ""
    if not _NAME_RE.fullmatch(name):
        return None

    raw_type = raw.get("type")
    if raw_type is None:
        field_type = "text"
    elif isinstance(raw_type, str) and raw_type.strip() in FIELD_TYPES:
        field_type = raw_type.strip()
    else:
        # Guessing a control for a type this runtime does not know would
        # collect the wrong kind of value.
        return None

    required = raw.get("required")
    field: dict[str, Any] = {
        "name": name,
        "label": _clean(raw.get("label"), MAX_CARD_FIELD_CHARS) or name,
        "type": field_type,
        "placeholder": _clean(raw.get("placeholder"), MAX_CARD_FIELD_CHARS),
        "required": required if isinstance(required, bool) else True,
    }

    # Set for a choice only: the values on offer, and whether it takes one.
    allowed: set[str] | None = None
    single = False
    if field_type == "text":
        field.update(_source_keys(raw))
    elif field_type == "choice":
        options = _normalize_options(raw.get("options"))
        if not options:
            # A choice with nothing to choose from cannot be answered.
            return None
        field["options"] = options
        field["multiple"] = raw.get("multiple") is True
        allowed = {option["value"] for option in options}
        single = not field["multiple"]
    elif field_type == "file":
        accept = _normalize_accept(raw.get("accept"))
        if accept:
            field["accept"] = accept

    default = _normalize_default(raw.get("default"), allowed, single=single)
    if default:
        field["default"] = default
    return field


def _normalize_options(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    options: list[dict[str, str]] = []
    for raw_option in raw[:MAX_FIELD_OPTIONS]:
        if isinstance(raw_option, Mapping):
            value = _clean(raw_option.get("value"), MAX_FIELD_OPTION_CHARS)
            label = _clean(raw_option.get("label"), MAX_FIELD_OPTION_CHARS) or value
        else:
            value = label = _clean(raw_option, MAX_FIELD_OPTION_CHARS)
        if value:
            options.append({"value": value, "label": label})
    return options


def _normalize_default(raw: Any, allowed: set[str] | None, *, single: bool) -> str | list[str]:
    """A field's preset value, in the shape it was written in.

    A string stays a string and a list stays a list. `allowed` is the option
    values of a choice field (None for any other type): a preset that is not
    on offer could never be shown as selected. `single` marks a choice that
    takes one value, which keeps only the first of a list. An empty result
    means "no default".
    """
    # Compared against option values, so cut to the same length they were.
    limit = MAX_FIELD_OPTION_CHARS if allowed is not None else MAX_CARD_FIELD_CHARS
    if isinstance(raw, str):
        value = _clean(raw, limit)
        return value if allowed is None or value in allowed else ""
    if not isinstance(raw, list):
        return []
    values = [value for value in (_clean(item, limit) for item in raw[:MAX_FIELD_DEFAULTS]) if value]
    if allowed is not None:
        values = [value for value in values if value in allowed]
    return values[:1] if single else values


def _normalize_accept(raw: Any) -> list[str]:
    """File extensions a file field takes, lower-cased (".csv")."""
    if not isinstance(raw, list):
        return []
    accept: list[str] = []
    for item in raw[:MAX_FIELD_ACCEPT]:
        if isinstance(item, str) and _ACCEPT_RE.fullmatch(item.strip()):
            accept.append(item.strip().lower())
    return accept


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
