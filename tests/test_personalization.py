"""Greeting and starter cards read from the agents-repo branch in the workspace.

Portal's init container already copies the agents repo's optional `portal/`
directory into the workspace for both runtimes; only the endpoint that serves it
was missing here, so an opencode assistant fell back to the generic welcome.
"""
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.personalization import (
    MAX_CARDS,
    MAX_WELCOME_CHARS,
    load_personalization,
)
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_portal(root: Path, *, welcome: str | None = None, cards: str | None = None) -> None:
    portal = root / "portal"
    portal.mkdir(parents=True, exist_ok=True)
    if welcome is not None:
        (portal / "welcome.md").write_text(welcome, encoding="utf-8")
    if cards is not None:
        (portal / "cards.yaml").write_text(cards, encoding="utf-8")


def test_cards_are_parsed_with_the_library_this_repository_declares():
    # The native runtime carries a sibling of this module and parses with
    # ruamel.yaml, because that is what *it* declares. This image installs
    # PyYAML and not ruamel, so copying that file across verbatim would import
    # a library that is never present -- the same shape of failure, mirrored.
    source = (REPO_ROOT / "efp_opencode_adapter" / "personalization.py").read_text(encoding="utf-8")

    imports = [line.strip() for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert "import yaml" in imports
    assert not [line for line in imports if "ruamel" in line]

    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
    assert "pyyaml" in pyproject
    assert "ruamel" not in pyproject, (
        "if ruamel.yaml is ever declared, say so here rather than letting the "
        "two runtimes drift apart silently"
    )


def test_missing_portal_directory_yields_empty_personalization(tmp_path):
    assert load_personalization(tmp_path) == {"welcome": None, "cards": []}


def test_welcome_and_cards_are_loaded(tmp_path):
    _write_portal(
        tmp_path,
        welcome="Hello there.",
        cards="""
cards:
  - title: Draft test cases
    description: From a ticket.
    icon: clipboard-check
    input:
      label: Ticket
      placeholder: ABC-1
    prompt: |
      Design test cases for {{input}}.
""",
    )

    result = load_personalization(tmp_path)

    assert result["welcome"] == "Hello there."
    card = result["cards"][0]
    assert card["title"] == "Draft test cases"
    assert card["icon"] == "clipboard-check"
    assert card["input"] == {"label": "Ticket", "placeholder": "ABC-1"}
    assert "{{input}}" in card["prompt"]


def test_card_without_title_or_prompt_is_dropped(tmp_path):
    # A card with no title has nothing to click; one with no prompt does
    # nothing when clicked. Neither should reach the UI.
    _write_portal(
        tmp_path,
        cards="""
cards:
  - description: no title here
    prompt: do something
  - title: no prompt here
    description: nothing happens
  - title: Valid
    prompt: go
""",
    )

    assert [c["title"] for c in load_personalization(tmp_path)["cards"]] == ["Valid"]


def test_card_without_icon_gets_a_default(tmp_path):
    _write_portal(tmp_path, cards="cards:\n  - title: T\n    prompt: P\n")

    assert load_personalization(tmp_path)["cards"][0]["icon"] == "sparkles"


def test_malformed_yaml_is_ignored_rather_than_raising(tmp_path):
    # A bad commit in the agents repo must degrade the panel, never break chat.
    _write_portal(tmp_path, welcome="Still here.", cards="cards: [ unclosed")

    result = load_personalization(tmp_path)

    assert result["welcome"] == "Still here."
    assert result["cards"] == []


def test_bare_list_without_cards_key_is_accepted(tmp_path):
    _write_portal(tmp_path, cards="- title: T\n  prompt: P\n")

    assert [c["title"] for c in load_personalization(tmp_path)["cards"]] == ["T"]


def test_oversized_content_is_capped(tmp_path):
    _write_portal(
        tmp_path,
        welcome="x" * (MAX_WELCOME_CHARS + 500),
        cards="cards:\n"
        + "".join(f"  - title: T{i}\n    prompt: P{i}\n" for i in range(MAX_CARDS + 5)),
    )

    result = load_personalization(tmp_path)

    assert len(result["welcome"]) == MAX_WELCOME_CHARS
    assert len(result["cards"]) == MAX_CARDS


@pytest.mark.parametrize("payload", ["", "   \n", "cards:\n"])
def test_empty_or_valueless_cards_file_yields_no_cards(tmp_path, payload):
    _write_portal(tmp_path, cards=payload)

    assert load_personalization(tmp_path)["cards"] == []


def test_a_scalar_card_entry_is_dropped_rather_than_crashing(tmp_path):
    # PyYAML happily parses a list of strings; a card must be a mapping.
    _write_portal(tmp_path, cards="cards:\n  - just a string\n  - title: T\n    prompt: P\n")

    assert [c["title"] for c in load_personalization(tmp_path)["cards"]] == ["T"]


# ------------------------------------------------------------------- endpoint


async def _client() -> TestClient:
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_endpoint_serves_the_workspace_portal_directory():
    settings = Settings.from_env()
    _write_portal(settings.workspace_dir, welcome="Hi.", cards="cards:\n  - title: T\n    prompt: P\n")
    client = await _client()

    resp = await client.get("/api/personalization")
    body = await resp.json()
    await client.close()

    assert resp.status == 200
    assert body["welcome"] == "Hi."
    assert [c["title"] for c in body["cards"]] == ["T"]


async def test_endpoint_answers_with_empty_personalization_when_the_branch_has_none():
    # A behavior pack branch predating portal/ must still serve a valid body,
    # so Portal can fall back to its generic welcome instead of erroring.
    client = await _client()

    resp = await client.get("/api/personalization")
    body = await resp.json()
    await client.close()

    assert resp.status == 200
    assert body == {"welcome": None, "cards": []}
