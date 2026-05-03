import json

from efp_opencode_adapter.init_assets import init_assets
from efp_opencode_adapter.settings import Settings


def test_init_assets_creates_dirs_and_config(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    skills = tmp_path / "skills"
    tools = tmp_path / "tools"
    state = tmp_path / "state"
    config = workspace / ".opencode" / "opencode.json"

    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_SKILLS_DIR", str(skills))
    monkeypatch.setenv("EFP_TOOLS_DIR", str(tools))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(config))

    (skills / "sample-skill").mkdir(parents=True, exist_ok=True)
    (skills / "sample-skill" / "skill.md").write_text("---\nname: sample-skill\ndescription: Sample\n---\n\nBody\n", encoding="utf-8")

    settings = Settings.from_env()
    init_assets(settings)

    assert (workspace / ".opencode").exists()
    assert (workspace / ".opencode" / "skills").exists()
    assert (workspace / ".opencode" / "tools").exists()
    assert (workspace / ".opencode" / "agents").exists()
    assert (workspace / ".opencode" / "agents" / "efp-main.md").exists()
    assert config.exists()
    assert (workspace / ".opencode" / "skills" / "sample-skill" / "SKILL.md").exists()
    assert (state / "skills-index.json").exists()

    payload = json.loads(config.read_text())
    assert payload["autoupdate"] is False
    assert payload["share"] == "disabled"
    assert payload["server"]["hostname"] == "127.0.0.1"
    assert payload["server"]["port"] == 4096
    assert payload["permission"]["*"] == "ask"
    assert payload["permission"]["external_directory"] == "deny"
    assert payload["permission"]["bash"]["rm *"] == "deny"
    assert payload["permission"]["bash"]["sudo *"] == "deny"
    assert payload["permission"]["bash"]["git push *"] == "deny"
    assert "efp-main" in payload["agent"]


def test_init_assets_does_not_overwrite_existing_config(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    skills = tmp_path / "skills"
    tools = tmp_path / "tools"
    state = tmp_path / "state"
    config = workspace / ".opencode" / "opencode.json"

    config.parent.mkdir(parents=True, exist_ok=True)
    sentinel = {"existing": True, "permission": {"*": "deny"}}
    config.write_text(json.dumps(sentinel), encoding="utf-8")

    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_SKILLS_DIR", str(skills))
    monkeypatch.setenv("EFP_TOOLS_DIR", str(tools))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(config))

    (skills / "existing-sync").mkdir(parents=True, exist_ok=True)
    (skills / "existing-sync" / "skill.md").write_text("---\nname: existing-sync\ndescription: Existing\n---\n\nBody\n", encoding="utf-8")

    init_assets(Settings.from_env())

    assert json.loads(config.read_text(encoding="utf-8")) == sentinel
    assert (workspace / ".opencode" / "skills" / "existing-sync" / "SKILL.md").exists()
    assert (state / "skills-index.json").exists()
    assert (workspace / ".opencode" / "skills").exists()
    assert (workspace / ".opencode" / "tools").exists()
    assert (workspace / ".opencode" / "agents").exists()
