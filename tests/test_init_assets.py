import json

from efp_opencode_adapter.init_assets import init_assets
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.workspace_gitignore import ensure_workspace_gitignore


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


def test_init_assets_provisions_workspace_gitignore_when_absent(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_SKILLS_DIR", str(tmp_path / "missing-skills"))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode" / "opencode.json"))

    init_assets(Settings.from_env())

    content = (workspace / ".gitignore").read_text(encoding="utf-8")
    # The heavy trees that dominate the OpenCode workspace snapshot on the PVC.
    for entry in ("node_modules/", "target/", ".venv/", "build/", "dist/", ".m2/"):
        assert entry in content.splitlines()
    assert list(workspace.glob(".gitignore.tmp")) == []


def test_init_assets_never_overwrites_an_existing_workspace_gitignore(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_SKILLS_DIR", str(tmp_path / "missing-skills"))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode" / "opencode.json"))
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ".gitignore").write_text("# mine\nsecret-notes/\n", encoding="utf-8")

    init_assets(Settings.from_env())

    assert (workspace / ".gitignore").read_text(encoding="utf-8") == "# mine\nsecret-notes/\n"


def test_ensure_workspace_gitignore_is_idempotent(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    settings = Settings.from_env()

    first = ensure_workspace_gitignore(settings)
    generated = first.read_text(encoding="utf-8")
    second = ensure_workspace_gitignore(settings)

    assert first == second == workspace / ".gitignore"
    assert second.read_text(encoding="utf-8") == generated


def test_init_assets_copies_skill_resources(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    skills = tmp_path / "skills"
    state = tmp_path / "state"
    config = workspace / ".opencode" / "opencode.json"

    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_SKILLS_DIR", str(skills))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(config))

    source = skills / "demo"
    (source / "scripts").mkdir(parents=True, exist_ok=True)
    (source / "SKILL.md").write_text("---\nname: demo\ndescription: Demo\n---\n\nBody\n", encoding="utf-8")
    (source / "scripts" / "run.py").write_text("print('run')\n", encoding="utf-8")

    init_assets(Settings.from_env())

    target = workspace / ".opencode" / "skills" / "demo"
    assert (target / "SKILL.md").exists()
    assert (target / "scripts" / "run.py").exists()
    assert (target / "skill.md").exists() is False
