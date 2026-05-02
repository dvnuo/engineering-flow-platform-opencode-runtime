import json
from pathlib import Path

import pytest
import yaml

from efp_opencode_adapter.skill_sync import sync_skills


def _write_skill(path: Path, frontmatter: dict, body: str = "Body") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    path.write_text(f"---\n{fm}\n---\n\n{body}\n", encoding="utf-8")


def test_skill_sync_core_behaviors(tmp_path):
    skills_dir = tmp_path / "skills"
    opencode_skills_dir = tmp_path / "workspace" / ".opencode" / "skills"
    state_dir = tmp_path / "state"

    _write_skill(
        skills_dir / "review-pull-request" / "skill.md",
        {
            "name": "review-pull-request",
            "description": "Review a PR",
            "tools": ["github_get_pr", "github_get_pr_files"],
            "task_tools": ["run_command"],
            "risk_level": "medium",
            "planning_mode": "strict",
            "strategy": "high-signal",
            "references": ["a", "b"],
        },
    )
    _write_skill(
        skills_dir / "collect_requirements_to_bundle" / "skill.md",
        {
            "name": "collect_requirements_to_bundle",
            "description": "Collect requirements",
            "tools": [],
            "task_tools": [],
        },
    )
    _write_skill(
        skills_dir / "create-pull-request" / "skill.md",
        {
            "name": "create-pull-request",
            "description": "Create a PR",
            "task_tools": ["run_command"],
        },
    )
    (skills_dir / "README.md").write_text("# README\n", encoding="utf-8")
    _write_skill(
        skills_dir / "legacy_skill.md",
        {"name": "legacy_skill", "description": "Legacy root skill"},
    )

    index = sync_skills(skills_dir, opencode_skills_dir, state_dir)

    assert any(x.opencode_name == "review-pull-request" for x in index.skills)
    underscore = next(x for x in index.skills if x.efp_name == "collect_requirements_to_bundle")
    assert underscore.opencode_name == "collect-requirements-to-bundle"

    review = next(x for x in index.skills if x.opencode_name == "review-pull-request")
    assert review.tools == ["github_get_pr", "github_get_pr_files"]
    assert review.task_tools == ["run_command"]
    assert review.risk_level == "medium"

    review_skill_md = opencode_skills_dir / "review-pull-request" / "SKILL.md"
    payload = _parse_frontmatter(review_skill_md.read_text(encoding="utf-8"))
    assert payload["name"] == "review-pull-request"
    assert payload["metadata"]["efp_tools"] == "github_get_pr,github_get_pr_files"
    assert payload["metadata"]["efp_task_tools"] == "run_command"
    assert payload["metadata"]["efp_extra"]["planning_mode"] == "strict"
    assert payload["metadata"]["efp_extra"]["strategy"] == "high-signal"
    assert payload["metadata"]["efp_extra"]["references"] == ["a", "b"]

    index_path = state_dir / "skills-index.json"
    assert index_path.exists()
    index_json = json.loads(index_path.read_text(encoding="utf-8"))
    assert index_json["generated_at"]
    assert len(index_json["skills"]) == 4
    for item in index_json["skills"]:
        assert Path(item["source_path"]).is_absolute()
        assert Path(item["target_path"]).is_absolute()

    assert (opencode_skills_dir.parent / "agents" / "skill-review-pull-request.md").exists()
    assert (opencode_skills_dir.parent / "agents" / "skill-create-pull-request.md").exists()
    assert not (opencode_skills_dir / "README" / "SKILL.md").exists()


def test_duplicate_normalized_name_raises(tmp_path):
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir / "foo_bar" / "skill.md", {"name": "foo_bar", "description": "a"})
    _write_skill(skills_dir / "foo-bar" / "skill.md", {"name": "foo-bar", "description": "b"})

    with pytest.raises(ValueError, match="duplicate normalized skill name"):
        sync_skills(skills_dir, tmp_path / "workspace/.opencode/skills", tmp_path / "state")


def test_missing_description_warning_and_fallback(tmp_path):
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir / "no-desc" / "skill.md", {"name": "no-desc"})

    with pytest.warns(UserWarning, match="missing description"):
        index = sync_skills(skills_dir, tmp_path / "workspace/.opencode/skills", tmp_path / "state")

    entry = index.skills[0]
    assert entry.description == "EFP skill no-desc"
    generated = (tmp_path / "workspace/.opencode/skills/no-desc/SKILL.md").read_text(encoding="utf-8")
    payload = _parse_frontmatter(generated)
    assert payload["description"] == "EFP skill no-desc"


def _parse_frontmatter(text: str) -> dict:
    end = text.find("\n---\n", 4)
    return yaml.safe_load(text[4:end])


def test_stale_generated_skill_is_removed(tmp_path):
    skills_dir = tmp_path / "skills"
    opencode_skills_dir = tmp_path / "workspace" / ".opencode" / "skills"
    state_dir = tmp_path / "state"

    stale_dir = opencode_skills_dir / "old-skill"
    stale_dir.mkdir(parents=True, exist_ok=True)
    (stale_dir / "SKILL.md").write_text("This skill was generated from an EFP skill asset.\n", encoding="utf-8")

    _write_skill(
        skills_dir / "new-skill" / "skill.md",
        {"name": "new-skill", "description": "New skill"},
    )

    sync_skills(skills_dir, opencode_skills_dir, state_dir)

    assert not stale_dir.exists()
    assert (opencode_skills_dir / "new-skill" / "SKILL.md").exists()

    payload = json.loads((state_dir / "skills-index.json").read_text(encoding="utf-8"))
    assert [x["opencode_name"] for x in payload["skills"]] == ["new-skill"]


def test_manual_opencode_skill_is_not_removed(tmp_path):
    skills_dir = tmp_path / "skills"
    opencode_skills_dir = tmp_path / "workspace" / ".opencode" / "skills"
    state_dir = tmp_path / "state"

    manual_dir = opencode_skills_dir / "manual-skill"
    manual_dir.mkdir(parents=True, exist_ok=True)
    (manual_dir / "SKILL.md").write_text("# Manual skill\n", encoding="utf-8")

    _write_skill(
        skills_dir / "new-skill" / "skill.md",
        {"name": "new-skill", "description": "New skill"},
    )

    sync_skills(skills_dir, opencode_skills_dir, state_dir)

    assert (manual_dir / "SKILL.md").exists()
    assert (opencode_skills_dir / "new-skill" / "SKILL.md").exists()


def test_missing_skills_dir_writes_empty_index(tmp_path):
    skills_dir = tmp_path / "does-not-exist"
    opencode_skills_dir = tmp_path / "workspace" / ".opencode" / "skills"
    state_dir = tmp_path / "state"

    with pytest.warns(UserWarning, match="skills directory does not exist"):
        index = sync_skills(skills_dir, opencode_skills_dir, state_dir)

    payload = json.loads((state_dir / "skills-index.json").read_text(encoding="utf-8"))
    assert payload["skills"] == []
    assert index.warnings
