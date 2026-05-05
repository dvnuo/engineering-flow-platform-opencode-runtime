import json

import pytest

from efp_opencode_adapter.init_assets import init_assets
from efp_opencode_adapter.settings import Settings


def test_init_assets_creates_dirs_and_config(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    skills = tmp_path / "skills"
    tools = tmp_path / "missing-tools"
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
    with pytest.warns(UserWarning, match="tools manifest not found"):
        init_assets(settings)

    assert (workspace / ".opencode").exists()
    assert (workspace / ".opencode" / "skills").exists()
    assert (workspace / ".opencode" / "tools").exists()
    assert (workspace / ".opencode" / "agents").exists()
    assert (workspace / ".opencode" / "agents" / "efp-main.md").exists()
    assert config.exists()
    assert (workspace / ".opencode" / "skills" / "sample-skill" / "SKILL.md").exists()
    assert (state / "skills-index.json").exists()
    assert (state / "tools-index.json").exists()
    tools_index = json.loads((state / "tools-index.json").read_text(encoding="utf-8"))
    assert tools_index["tools"] == []

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
    tools.mkdir(parents=True, exist_ok=True)
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


def test_init_assets_syncs_tools_with_generator(tmp_path, monkeypatch):
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

    tools.mkdir(parents=True, exist_ok=True)
    (tools / "manifest.yaml").write_text("tools: []\n", encoding="utf-8")
    generator = tools / "adapters" / "opencode" / "generate_tools.py"
    generator.parent.mkdir(parents=True, exist_ok=True)
    generator.write_text(
        """
import argparse, json
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument('--tools-dir', required=True)
p.add_argument('--opencode-tools-dir', required=True)
p.add_argument('--state-dir', required=True)
a=p.parse_args()
Path(a.opencode_tools_dir).mkdir(parents=True, exist_ok=True)
(Path(a.opencode_tools_dir)/'efp_context_echo.ts').write_text('//wrapper', encoding='utf-8')
Path(a.state_dir).mkdir(parents=True, exist_ok=True)
(Path(a.state_dir)/'tools-index.json').write_text(json.dumps({'generated_at':'now','tools':[{'opencode_name':'efp_context_echo'}]}), encoding='utf-8')
""",
        encoding="utf-8",
    )

    init_assets(Settings.from_env())

    assert (workspace / ".opencode" / "tools" / "efp_context_echo.ts").exists()
    tools_index = json.loads((state / "tools-index.json").read_text(encoding="utf-8"))
    assert tools_index["tools"][0]["opencode_name"] == "efp_context_echo"


def test_init_assets_creates_tools_dir_from_env_override(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    skills = tmp_path / "skills"
    tools = tmp_path / "custom-tools"
    state = tmp_path / "state"
    config = workspace / ".opencode" / "opencode.json"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_SKILLS_DIR", str(skills))
    monkeypatch.setenv("EFP_TOOLS_DIR", str(tools))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCODE_CONFIG", str(config))
    with pytest.warns(UserWarning):
        init_assets(Settings.from_env())
    assert tools.exists()


def test_init_assets_syncs_tools_before_skills(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("EFP_SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setenv("EFP_TOOLS_DIR", str(tmp_path / "tools"))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENCODE_CONFIG", str(tmp_path / "workspace/.opencode/opencode.json"))

    def fake_sync_tools(*args, **kwargs):
        calls.append("tools")
        return {"tools": [{"legacy_name": "legacy", "opencode_name": "efp_legacy"}]}

    def fake_sync_skills(*args, **kwargs):
        calls.append("skills")
        assert kwargs.get("tools_index")
        return None

    monkeypatch.setattr("efp_opencode_adapter.init_assets.sync_tools", fake_sync_tools)
    monkeypatch.setattr("efp_opencode_adapter.init_assets.sync_skills", fake_sync_skills)
    init_assets(Settings.from_env())
    assert calls[:2] == ["tools", "skills"]
