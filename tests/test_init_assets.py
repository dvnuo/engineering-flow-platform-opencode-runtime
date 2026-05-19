import json

from efp_opencode_adapter.init_assets import init_assets
from efp_opencode_adapter.settings import Settings


def test_init_assets_creates_skills_and_config_without_external_tools(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    skills = tmp_path / "skills"
    state = tmp_path / "state"
    config = workspace / ".opencode" / "opencode.json"

    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_SKILLS_DIR", str(skills))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(config))
    monkeypatch.setenv("EFP_TOOLS_DIR", str(tmp_path / "legacy-tools"))

    (skills / "sample-skill").mkdir(parents=True, exist_ok=True)
    (skills / "sample-skill" / "skill.md").write_text("---\nname: sample-skill\ndescription: Sample\n---\n\nBody\n", encoding="utf-8")

    init_assets(Settings.from_env())

    assert (workspace / ".opencode" / "skills" / "sample-skill" / "SKILL.md").exists()
    assert (state / "skills-index.json").exists()
    assert (workspace / ".opencode" / "tools").exists() is False
    assert (state / "tools-index.json").exists() is False


def test_init_assets_missing_skills_dir_still_boots(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    config = workspace / ".opencode" / "opencode.json"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_SKILLS_DIR", str(tmp_path / "missing-skills"))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(config))

    init_assets(Settings.from_env())
    assert config.exists()
    payload = json.loads(config.read_text(encoding="utf-8"))
    assert "permission" in payload and "agent" in payload


def test_init_assets_copies_skill_resources(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    skills = tmp_path / "skills"
    state = tmp_path / "state"
    config = workspace / ".opencode" / "opencode.json"

    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_SKILLS_DIR", str(skills))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(config))

    (skills / "sample-skill" / "scripts").mkdir(parents=True, exist_ok=True)
    (skills / "sample-skill" / "SKILL.md").write_text("---\nname: sample-skill\ndescription: Sample\n---\n\nBody\n", encoding="utf-8")
    (skills / "sample-skill" / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")

    init_assets(Settings.from_env())

    target_dir = workspace / ".opencode" / "skills" / "sample-skill"
    target_skill = target_dir / "SKILL.md"
    assert target_skill.exists()
    assert (target_dir / "scripts" / "run.py").exists()
    assert str(target_dir.resolve()) in target_skill.read_text(encoding="utf-8")
