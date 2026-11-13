"""Greeting and starter cards read from the agents-repo branch in the workspace.

Portal's init container already copies the agents repo's optional `portal/`
directory into the workspace for both runtimes; only the endpoint that serves it
was missing here, so an opencode assistant fell back to the generic welcome.
"""
from pathlib import Path

import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.personalization import (
    MAX_CARD_FIELD_CHARS,
    MAX_CARDS,
    MAX_PROMPT_CHARS,
    MAX_WELCOME_CHARS,
    load_personalization,
)
from efp_opencode_adapter.personalization_api import requested_card_version
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


# ----------------------------------------------------------- card contract v2
#
# Version 2 adds a source / params / multiline to `input`, and a form
# (`fields` + `template`) next to the legacy prompt. Everything invalid is
# dropped, never raised; the form is only ever sent to a caller that asked.

CONTRACT_EXAMPLE = """
cards:
  - title: Open a pull request
    description: Starts from a branch.
    icon: git-branch
    prompt: |
      Open a pull request for {{input}}.
    input:
      label: Branch
      placeholder: org/repo and branch
      source: github.branches
      params: { types: [Epic] }
      multiline: true
    fields:
      - name: story
        label: Jira story
        type: text
        placeholder: FX-12
        required: true
        source: mobile.stories
        params: { types: [Story] }
      - name: platforms
        label: Platforms
        type: choice
        multiple: true
        options: [android, ios]
        default: [android, ios]
      - name: csv
        label: CSV file
        type: file
        accept: [".csv"]
        required: false
    template: |
      Run the mobile scenarios of {{story}} on {{platforms}}.
"""

CONTRACT_EXAMPLE_LEGACY_CARD = {
    "title": "Open a pull request",
    "description": "Starts from a branch.",
    "icon": "git-branch",
    "prompt": "Open a pull request for {{input}}.",
    "input": {
        "label": "Branch",
        "placeholder": "org/repo and branch",
        "source": "github.branches",
        "params": {"types": ["Epic"]},
        "multiline": True,
    },
}

CONTRACT_EXAMPLE_FORM = {
    "fields": [
        {
            "name": "story",
            "label": "Jira story",
            "type": "text",
            "placeholder": "FX-12",
            "required": True,
            "source": "mobile.stories",
            "params": {"types": ["Story"]},
        },
        {
            "name": "platforms",
            "label": "Platforms",
            "type": "choice",
            "placeholder": "",
            "required": True,
            "options": [{"value": "android", "label": "android"}, {"value": "ios", "label": "ios"}],
            "multiple": True,
            "default": ["android", "ios"],
        },
        {
            "name": "csv",
            "label": "CSV file",
            "type": "file",
            "placeholder": "",
            "required": False,
            "accept": [".csv"],
        },
    ],
    "template": "Run the mobile scenarios of {{story}} on {{platforms}}.",
}

PLAIN_TEXT_FIELD = {"name": "story", "label": "story", "type": "text", "placeholder": "", "required": True}


def _cards(root: Path, *cards: dict, version: int = 2) -> list[dict]:
    """The normalized cards for card mappings written out as cards.yaml."""
    _write_portal(root, cards=yaml.safe_dump({"cards": list(cards)}, sort_keys=False))
    return load_personalization(root, version=version)["cards"]


def _input(root: Path, raw_input: dict) -> dict:
    """The normalized `input` of a legacy card, as a legacy caller gets it."""
    (card,) = _cards(root, {"title": "T", "prompt": "Do {{input}}.", "input": raw_input}, version=1)
    return card["input"]


def _field(root: Path, raw_field: dict) -> dict:
    """The normalized field of a one-field form card."""
    (card,) = _cards(root, {"title": "T", "fields": [raw_field], "template": "Go."})
    (field,) = card["fields"]
    return field


def test_contract_example_at_v2_carries_both_forms(tmp_path):
    _write_portal(tmp_path, cards=CONTRACT_EXAMPLE)

    cards = load_personalization(tmp_path, version=2)["cards"]

    assert cards == [{**CONTRACT_EXAMPLE_LEGACY_CARD, **CONTRACT_EXAMPLE_FORM}]


@pytest.mark.parametrize("version", [1, 0, -2])
def test_legacy_versions_never_receive_fields_or_template(tmp_path, version):
    _write_portal(tmp_path, cards=CONTRACT_EXAMPLE)

    # The new `input` keys are optional extras an older Portal ignores; the
    # form is a different way to send the card, so it is held back entirely.
    assert load_personalization(tmp_path, version=version)["cards"] == [CONTRACT_EXAMPLE_LEGACY_CARD]
    assert load_personalization(tmp_path)["cards"] == [CONTRACT_EXAMPLE_LEGACY_CARD]


def test_fields_only_card_is_omitted_at_v1_and_sent_with_an_empty_prompt_at_v2(tmp_path):
    card = {
        "title": "Run scenarios",
        # Without a legacy prompt there is no {{input}} for this to fill.
        "input": {"label": "Ignored"},
        "fields": [{"name": "story"}],
        "template": "Run {{story}}.",
    }

    assert _cards(tmp_path, card, version=1) == []
    assert _cards(tmp_path, card, version=2) == [
        {
            "title": "Run scenarios",
            "description": "",
            "icon": "sparkles",
            "prompt": "",
            "fields": [PLAIN_TEXT_FIELD],
            "template": "Run {{story}}.",
        }
    ]


def test_a_newer_version_than_this_runtime_knows_still_gets_the_form(tmp_path):
    card = {"title": "T", "fields": [{"name": "story"}], "template": "Run {{story}}."}

    assert _cards(tmp_path, card, version=3) == _cards(tmp_path, card, version=2)


# -- input: source / params / multiline


@pytest.mark.parametrize("source", ["github.branches", "jira", "a1.b_2.c3", "  jira.issues  ", "s" * 64])
def test_input_source_is_kept_when_it_is_a_source_id(tmp_path, source):
    assert _input(tmp_path, {"label": "L", "source": source}) == {
        "label": "L",
        "placeholder": "",
        "source": source.strip(),
    }


@pytest.mark.parametrize(
    "source",
    [
        "GitHub.branches",
        "github branches",
        "github..branches",
        ".github",
        "github.",
        "1jira",
        "jira-issues",
        "s" * 65,
        "",
        7,
        True,
        None,
        ["jira.issues"],
    ],
)
def test_input_source_that_is_not_a_source_id_is_omitted(tmp_path, source):
    # The input stays what it always was: a plain text box.
    assert _input(tmp_path, {"label": "L", "source": source}) == {"label": "L", "placeholder": ""}


def test_params_keep_strings_booleans_integers_and_string_lists(tmp_path):
    params = {
        "project": "  FX  ",
        "open_only": True,
        "limit": 25,
        "types": [" Epic ", "", "Story", 5, None, "x" * 150],
        "summary": "y" * 250,
        "ratio": 0.5,
        "nested": {"a": 1},
        "nothing": None,
    }

    assert _input(tmp_path, {"source": "jira.issues", "params": params})["params"] == {
        "project": "FX",
        "open_only": True,
        "limit": 25,
        # Stripped, empties and non-strings dropped, each capped at 100.
        "types": ["Epic", "Story", "x" * 100],
        "summary": "y" * 200,
    }


def test_params_drop_entries_with_a_bad_key(tmp_path):
    params = {
        "ok_key_1": "kept",
        "k" * 32: "kept",
        "k" * 33: "too long",
        "Bad": "capital",
        "9lives": "leading digit",
        "with-dash": "dash",
        # PyYAML reads an unquoted number or `true` as a key of that type.
        7: "not a string",
        True: "not a string",
    }

    assert _input(tmp_path, {"params": params})["params"] == {"ok_key_1": "kept", "k" * 32: "kept"}


def test_params_are_capped_at_eight_entries_and_ten_list_items(tmp_path):
    many = _input(tmp_path, {"params": {f"k{i}": i for i in range(9)}})["params"]
    assert many == {f"k{i}": i for i in range(8)}

    long_list = _input(tmp_path, {"params": {"types": [f"t{i}" for i in range(12)]}})["params"]
    assert long_list == {"types": [f"t{i}" for i in range(10)]}


@pytest.mark.parametrize("params", [{}, {"Bad": "x", "ratio": 1.5}, "types=Epic", ["types"], 3, None])
def test_params_that_are_invalid_or_left_empty_are_omitted(tmp_path, params):
    assert _input(tmp_path, {"label": "L", "source": "jira.issues", "params": params}) == {
        "label": "L",
        "placeholder": "",
        "source": "jira.issues",
    }


@pytest.mark.parametrize(
    "value, emitted",
    [(True, True), (False, False), ("true", False), ("yes", False), (1, False), (None, False)],
)
def test_multiline_is_emitted_only_for_the_boolean_true(tmp_path, value, emitted):
    normalized = _input(tmp_path, {"label": "L", "multiline": value})

    assert normalized == ({"label": "L", "placeholder": "", "multiline": True} if emitted else {"label": "L", "placeholder": ""})


# -- fields + template: valid as a pair, or dropped as a pair

INVALID_FORMS = {
    "template names an undeclared field": ([{"name": "story"}], "Run {{story}} on {{platform}}."),
    "template uses the legacy placeholder": ([{"name": "story"}], "Run {{input}}."),
    "duplicate field names": ([{"name": "story"}, {"name": "story", "type": "textarea"}], "Run {{story}}."),
    "more than six fields": ([{"name": f"f{i}"} for i in range(7)], "Run."),
    "no fields": ([], "Run."),
    "fields is not a list": ({"name": "story"}, "Run {{story}}."),
    "a field that is not a mapping": ([{"name": "story"}, "platform"], "Run {{story}}."),
    "unknown field type": ([{"name": "story", "type": "date"}], "Run {{story}}."),
    "field type in the wrong case": ([{"name": "story", "type": "Text"}], "Run {{story}}."),
    "field type that is not a string": ([{"name": "story", "type": 3}], "Run {{story}}."),
    "choice without options": ([{"name": "os", "type": "choice"}], "Run on {{os}}."),
    "choice with no valid option": (
        [{"name": "os", "type": "choice", "options": ["", 3, {"label": "no value"}]}],
        "Run on {{os}}.",
    ),
    "choice whose options are not a list": ([{"name": "os", "type": "choice", "options": "ios"}], "Run on {{os}}."),
    "field without a name": ([{"label": "Story"}], "Run."),
    "field name with capitals": ([{"name": "Story"}], "Run."),
    "field name starting with a digit": ([{"name": "1story"}], "Run."),
    "field name over 32 characters": ([{"name": "s" * 33}], "Run."),
    "field name that is not a string": ([{"name": 5}], "Run."),
    "missing template": ([{"name": "story"}], None),
    "blank template": ([{"name": "story"}], "   "),
    "template that is not a string": ([{"name": "story"}], ["Run {{story}}."]),
}


@pytest.mark.parametrize("fields, template", list(INVALID_FORMS.values()), ids=list(INVALID_FORMS))
def test_invalid_form_is_dropped_as_a_pair(tmp_path, fields, template):
    form = {"fields": fields} if template is None else {"fields": fields, "template": template}

    cards = _cards(
        tmp_path,
        {"title": "Has a legacy prompt", "prompt": "Do {{input}}.", **form},
        {"title": "Form only", **form},
    )

    # The first card falls back to its legacy prompt, with neither half of
    # the form; the second has nothing left to send and is not a card at all.
    assert cards == [{"title": "Has a legacy prompt", "description": "", "icon": "sparkles", "prompt": "Do {{input}}."}]


def test_six_fields_and_a_field_no_placeholder_uses_are_valid(tmp_path):
    fields = [{"name": f"f{i}"} for i in range(5)] + [{"name": "attachment", "type": "file"}]

    (card,) = _cards(tmp_path, {"title": "T", "fields": fields, "template": "Use {{f0}} and {{ f4 }}."})

    assert [field["name"] for field in card["fields"]] == ["f0", "f1", "f2", "f3", "f4", "attachment"]
    assert card["template"] == "Use {{f0}} and {{ f4 }}."


def test_template_is_stripped_and_capped_like_a_prompt(tmp_path):
    (card,) = _cards(tmp_path, {"title": "T", "fields": [{"name": "story"}], "template": "  {{story}} " + "x" * 5000})

    assert card["template"].startswith("{{story}} x")
    assert len(card["template"]) == MAX_PROMPT_CHARS


def test_field_defaults(tmp_path):
    # Missing type means text, the label falls back to the name, the
    # placeholder is always present, and a field is required unless it says no.
    assert _field(tmp_path, {"name": "story"}) == PLAIN_TEXT_FIELD
    assert _field(tmp_path, {"name": "s" * 32})["name"] == "s" * 32


@pytest.mark.parametrize("value, required", [(False, False), (True, True), ("no", True), (0, True), (None, True)])
def test_field_required_is_a_boolean_that_defaults_to_true(tmp_path, value, required):
    assert _field(tmp_path, {"name": "story", "required": value})["required"] is required


def test_field_label_and_placeholder_are_capped(tmp_path):
    field = _field(tmp_path, {"name": "story", "label": "l" * 600, "placeholder": "p" * 600})

    assert field["label"] == "l" * MAX_CARD_FIELD_CHARS
    assert field["placeholder"] == "p" * MAX_CARD_FIELD_CHARS


def test_field_source_and_params_follow_the_input_rules(tmp_path):
    field = _field(
        tmp_path,
        {"name": "story", "type": "text", "source": "jira.issues", "params": {"types": ["Story"], "Bad": 1}},
    )
    assert (field["source"], field["params"]) == ("jira.issues", {"types": ["Story"]})

    plain = _field(tmp_path, {"name": "story", "source": "Not A Source", "params": {}})
    assert plain == PLAIN_TEXT_FIELD


@pytest.mark.parametrize("field_type", ["textarea", "choice", "file"])
def test_field_source_and_params_are_only_kept_for_text(tmp_path, field_type):
    field = _field(
        tmp_path,
        {
            "name": "thing",
            "type": field_type,
            "options": ["a"],
            "source": "jira.issues",
            "params": {"types": ["Story"]},
        },
    )

    assert field["type"] == field_type
    assert "source" not in field and "params" not in field


def test_choice_options_accept_strings_and_value_label_mappings(tmp_path):
    field = _field(
        tmp_path,
        {
            "name": "os",
            "type": "choice",
            "options": [
                "android",
                "  ios  ",
                {"value": "web", "label": "Web app"},
                {"value": "tv"},
                {"value": "watch", "label": 7},
                "",
                7,
                None,
                {"label": "no value"},
                {"value": "  "},
                "v" * 250,
                {"value": "long", "label": "L" * 250},
            ],
        },
    )

    assert field["options"] == [
        {"value": "android", "label": "android"},
        {"value": "ios", "label": "ios"},
        {"value": "web", "label": "Web app"},
        {"value": "tv", "label": "tv"},
        {"value": "watch", "label": "watch"},
        {"value": "v" * 200, "label": "v" * 200},
        {"value": "long", "label": "L" * 200},
    ]
    assert field["multiple"] is False


def test_choice_options_are_capped_at_twenty(tmp_path):
    field = _field(tmp_path, {"name": "n", "type": "choice", "options": [f"o{i}" for i in range(25)]})

    assert [option["value"] for option in field["options"]] == [f"o{i}" for i in range(20)]


@pytest.mark.parametrize("value, multiple", [(True, True), (False, False), ("true", False), (1, False)])
def test_multiple_is_a_boolean_that_defaults_to_false(tmp_path, value, multiple):
    assert _field(tmp_path, {"name": "os", "type": "choice", "options": ["a"], "multiple": value})["multiple"] is multiple


@pytest.mark.parametrize("field_type", ["text", "textarea", "file"])
def test_choice_only_keys_are_not_emitted_for_other_types(tmp_path, field_type):
    field = _field(
        tmp_path,
        {"name": "thing", "type": field_type, "options": ["a", "b"], "multiple": True},
    )

    assert "options" not in field and "multiple" not in field


def test_multiple_choice_defaults_are_filtered_to_the_option_values(tmp_path):
    field = _field(
        tmp_path,
        {
            "name": "os",
            "type": "choice",
            "multiple": True,
            "options": ["android", {"value": "ios", "label": "iOS"}],
            "default": ["ios", "windows", "iOS", " android ", 3],
        },
    )

    assert field["default"] == ["ios", "android"]


def test_single_choice_keeps_only_the_first_valid_default(tmp_path):
    choice = {"name": "os", "type": "choice", "options": ["android", "ios"]}

    assert _field(tmp_path, {**choice, "default": ["windows", "ios", "android"]})["default"] == ["ios"]
    assert _field(tmp_path, {**choice, "default": "ios"})["default"] == "ios"


@pytest.mark.parametrize("default", ["windows", ["windows", "linux"], [], "", 3, None, {"value": "ios"}])
def test_choice_default_is_omitted_when_nothing_valid_is_left(tmp_path, default):
    field = _field(tmp_path, {"name": "os", "type": "choice", "options": ["android", "ios"], "default": default})

    assert "default" not in field


def test_text_default_is_a_string_or_a_list_of_at_most_twenty_strings(tmp_path):
    assert _field(tmp_path, {"name": "story", "default": "  FX-12  "})["default"] == "FX-12"
    assert _field(tmp_path, {"name": "story", "default": ["FX-1", "", 2, " FX-2 "]})["default"] == ["FX-1", "FX-2"]
    many = _field(tmp_path, {"name": "story", "default": [f"FX-{i}" for i in range(25)]})["default"]
    assert many == [f"FX-{i}" for i in range(20)]
    for empty in ("   ", [], ["", 4], 12, None):
        assert "default" not in _field(tmp_path, {"name": "story", "default": empty})


def test_file_accept_is_a_list_of_lower_cased_extensions(tmp_path):
    field = _field(
        tmp_path,
        {
            "name": "csv",
            "type": "file",
            "accept": [".CSV", " .Xlsx ", "csv", ".tar.gz", ".", "text/csv", 5, ".abcdefghijk"],
        },
    )

    assert field["accept"] == [".csv", ".xlsx"]


def test_file_accept_is_capped_at_eight_and_omitted_when_empty(tmp_path):
    many = _field(tmp_path, {"name": "f", "type": "file", "accept": [f".e{i}" for i in range(10)]})
    assert many["accept"] == [f".e{i}" for i in range(8)]

    for empty in ([], ["csv"], ".csv", None):
        assert "accept" not in _field(tmp_path, {"name": "f", "type": "file", "accept": empty})


def test_accept_is_only_kept_for_file_fields(tmp_path):
    assert "accept" not in _field(tmp_path, {"name": "story", "type": "text", "accept": [".csv"]})


@pytest.mark.parametrize(
    "raw, version",
    [
        (None, 1),
        ("", 1),
        ("1", 1),
        ("0", 1),
        ("-2", 1),
        ("2.0", 1),
        ("two", 1),
        ("2x", 1),
        ("9" * 5000, 1),
        ("2", 2),
        (" 2 ", 2),
        ("3", 3),
    ],
)
def test_only_an_integer_of_two_or_more_asks_for_the_form(raw, version):
    assert requested_card_version(raw) == version


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


FORM_ONLY_CARD = """
  - title: Run scenarios
    fields:
      - name: story
    template: Run {{story}}.
"""


async def test_endpoint_sends_the_form_only_to_a_caller_that_asks_with_v2():
    settings = Settings.from_env()
    _write_portal(settings.workspace_dir, welcome="Hi.", cards=CONTRACT_EXAMPLE + FORM_ONLY_CARD)
    client = await _client()

    legacy = await (await client.get("/api/personalization")).json()
    resp = await client.get("/api/personalization", params={"v": "2"})
    v2 = await resp.json()
    await client.close()

    assert resp.status == 200
    # A Portal that predates the parameter gets the cards it always got: the
    # legacy shape, and no card it would have no prompt to send for.
    assert legacy == {"welcome": "Hi.", "cards": [CONTRACT_EXAMPLE_LEGACY_CARD]}
    assert v2 == {
        "welcome": "Hi.",
        "cards": [
            {**CONTRACT_EXAMPLE_LEGACY_CARD, **CONTRACT_EXAMPLE_FORM},
            {
                "title": "Run scenarios",
                "description": "",
                "icon": "sparkles",
                "prompt": "",
                "fields": [PLAIN_TEXT_FIELD],
                "template": "Run {{story}}.",
            },
        ],
    }


@pytest.mark.parametrize("v", ["1", "0", "-2", "2.0", "abc", ""])
async def test_endpoint_treats_any_other_v_as_the_legacy_contract(v):
    settings = Settings.from_env()
    _write_portal(settings.workspace_dir, cards=CONTRACT_EXAMPLE + FORM_ONLY_CARD)
    client = await _client()

    resp = await client.get("/api/personalization", params={"v": v})
    body = await resp.json()
    await client.close()

    assert resp.status == 200
    assert body["cards"] == [CONTRACT_EXAMPLE_LEGACY_CARD]
