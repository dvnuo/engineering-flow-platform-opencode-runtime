"""Repo-relative skill paths, so Portal can link a slash command to its source.

Portal knows which skills repository and branch an assistant booted with, but
not where inside that checkout a given skill lives -- a skill's folder and its
declared name are allowed to differ, so the folder cannot be derived from the
name. The index records an absolute container path, which is no use as a link
and would put the image layout in front of every browser that loads the
composer, so the handler translates it.

The native runtime carries a sibling of this behaviour and answers with the
same `repo_path` field.
"""
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.compat_api import _repo_relative_skill_path, _resolved_skills_root
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings


class FakeClient:
    async def health(self):
        return {"healthy": True, "version": "1.14.39"}

    async def mcp(self):
        return {"success": True, "tools": []}


def _relative(source, skills_dir) -> str:
    """As the handler does it: resolve the root once, then place each skill."""
    return _repo_relative_skill_path(str(source), _resolved_skills_root(skills_dir))


def test_a_path_under_the_checkout_becomes_relative(tmp_path):
    skills = tmp_path / "skills"
    source = skills / "create-pull-request" / "skill.md"

    assert _relative(source, skills) == "create-pull-request/skill.md"


def test_a_skills_directory_that_is_not_there_yet_still_places_skills(tmp_path):
    # On a pod whose init container has not finished, nothing exists on disk
    # yet. Resolution has to stay non-strict, or listing skills becomes a 500
    # exactly while the assistant is starting.
    missing = tmp_path / "not-created-yet"

    assert _relative(missing / "a-skill" / "skill.md", missing) == "a-skill/skill.md"


def test_the_result_is_posix_so_a_windows_host_still_links_correctly(tmp_path):
    skills = tmp_path / "skills"
    source = skills / "nested" / "skill.md"

    assert "\\" not in _relative(source, skills)


def test_a_path_outside_the_checkout_gets_no_link(tmp_path):
    # Better no link than one pointing at a file the repository does not have.
    outside = tmp_path / "elsewhere" / "skill.md"

    assert _relative(outside, tmp_path / "skills") == ""


@pytest.mark.parametrize("value", [None, "", "   ", 17, [], {}])
def test_an_index_without_a_usable_source_path_is_survivable(tmp_path, value):
    # An index written by an older sync has no source_path at all; the skills
    # list must still render rather than 500.
    assert _repo_relative_skill_path(value, _resolved_skills_root(tmp_path)) == ""


def _index_entry(**overrides):
    entry = {
        "opencode_name": "my-skill",
        "description": "d",
        "efp_name": "e",
        "tools": [],
        "task_tools": [],
    }
    entry.update(overrides)
    return entry


async def _skills_payload(tmp_path, monkeypatch, entry):
    workspace, state, skills = tmp_path / "workspace", tmp_path / "state", tmp_path / "skills"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("EFP_SKILLS_DIR", str(skills))
    monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    state.mkdir(parents=True)
    (workspace / ".opencode").mkdir(parents=True)
    (state / "skills-index.json").write_text(json.dumps({"skills": [entry]}), encoding="utf-8")
    (workspace / ".opencode/opencode.json").write_text(
        json.dumps({"permission": {"skill": {"*": "allow"}}}), encoding="utf-8"
    )

    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=FakeClient())))
    await client.start_server()
    try:
        return (await (await client.get("/api/skills")).json())["skills"][0]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_the_endpoint_reports_the_repo_path(tmp_path, monkeypatch):
    source = tmp_path / "skills" / "design_test_cases_from_bundle" / "skill.md"
    skill = await _skills_payload(tmp_path, monkeypatch, _index_entry(source_path=str(source)))

    assert skill["repo_path"] == "design_test_cases_from_bundle/skill.md"


@pytest.mark.asyncio
async def test_the_repo_path_is_never_absolute(tmp_path, monkeypatch):
    source = tmp_path / "skills" / "some-skill" / "skill.md"
    skill = await _skills_payload(tmp_path, monkeypatch, _index_entry(source_path=str(source)))

    assert str(tmp_path) not in skill["repo_path"]


@pytest.mark.asyncio
async def test_an_entry_with_no_source_path_still_lists(tmp_path, monkeypatch):
    skill = await _skills_payload(tmp_path, monkeypatch, _index_entry())

    assert skill["name"] == "my-skill"
    assert skill["repo_path"] == ""
