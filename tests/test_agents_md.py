from pathlib import Path

from efp_opencode_adapter.agents_md import ensure_default_agents_md, read_agents_md, write_agents_md
from efp_opencode_adapter.settings import Settings


def _settings(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENCODE_CONFIG", str(tmp_path / "workspace/.opencode/opencode.json"))
    return Settings.from_env()


def test_workspace_agents_md_example_exists():
    path = Path("workspace/AGENTS.md.example")
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "# AGENTS.md" in content
    assert "EFP Portal" in content


def test_ensure_default_agents_md_creates_default_when_missing(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    p = ensure_default_agents_md(s)
    assert p.exists()
    assert p.read_text(encoding="utf-8") == Path("workspace/AGENTS.md.example").read_text(encoding="utf-8")


def test_ensure_default_agents_md_uses_workspace_example_when_missing(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    p = ensure_default_agents_md(s)
    assert p.read_text(encoding="utf-8") == Path("workspace/AGENTS.md.example").read_text(encoding="utf-8")


def test_ensure_default_agents_md_does_not_overwrite_existing(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    p = s.workspace_dir / "AGENTS.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("mine", encoding="utf-8")
    ensure_default_agents_md(s)
    assert p.read_text(encoding="utf-8") == "mine"


def test_ensure_default_agents_md_migrates_legacy_adapter_agents_prompt(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    legacy = s.adapter_state_dir / "system_prompts" / "agents.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("legacy-adapter", encoding="utf-8")
    assert ensure_default_agents_md(s).read_text(encoding="utf-8") == "legacy-adapter"


def test_ensure_default_agents_md_prefers_existing_over_legacy(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    p = s.workspace_dir / "AGENTS.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("existing", encoding="utf-8")
    (s.adapter_state_dir / "system_prompts").mkdir(parents=True, exist_ok=True)
    (s.adapter_state_dir / "system_prompts" / "agents.md").write_text("legacy", encoding="utf-8")
    ensure_default_agents_md(s)
    assert p.read_text(encoding="utf-8") == "existing"


def test_ensure_default_agents_md_ignores_legacy_efp_main_prompt(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    legacy = s.workspace_dir / ".opencode" / "agents" / "efp-main.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("legacy-main-should-not-be-used", encoding="utf-8")
    output = ensure_default_agents_md(s).read_text(encoding="utf-8")
    assert output == Path("workspace/AGENTS.md.example").read_text(encoding="utf-8")
    assert "legacy-main-should-not-be-used" not in output


def test_read_write_agents_md_roundtrip(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    write_agents_md(s, "abc")
    assert read_agents_md(s) == "abc"


def test_agents_md_module_does_not_inline_default_prompt():
    source = Path("efp_opencode_adapter/agents_md.py").read_text(encoding="utf-8")
    assert "DEFAULT_AGENTS_MD" not in source
    assert "This OpenCode runtime is managed by EFP Portal" not in source
    assert "efp-main.md" not in source
    assert "legacy_efp_main" not in source


def test_no_production_reference_to_legacy_efp_main_prompt():
    production_files = list(Path("efp_opencode_adapter").rglob("*.py"))
    combined = "\n".join(p.read_text(encoding="utf-8") for p in production_files)
    assert "legacy_efp_main" not in combined
    assert ".opencode/agents/efp-main.md" not in combined
    assert "efp-main.md" not in combined
