import json

from efp_opencode_adapter.opencode_config import build_opencode_config, model_from_runtime_profile, write_main_agent_prompt
from efp_opencode_adapter.settings import Settings


def test_build_opencode_config_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("OPENCODE_CONFIG", str(tmp_path / "workspace/.opencode/opencode.json"))
    cfg, _, updated = build_opencode_config(Settings.from_env(), None)
    assert cfg["autoupdate"] is False
    assert cfg["share"] == "disabled"
    assert cfg["server"] == {"hostname": "127.0.0.1", "port": 4096}
    assert "permission" in cfg and "efp-main" in cfg["agent"]
    assert "model" not in cfg["agent"]["efp-main"]
    assert "permission" in updated and "agent" in updated


def test_model_mapping():
    assert model_from_runtime_profile({"llm": {"provider": "anthropic", "model": "claude-sonnet-4-5"}}) == "anthropic/claude-sonnet-4-5"
    assert model_from_runtime_profile({"llm": {"provider": "openai", "model": "gpt-5.1"}}) == "openai/gpt-5.1"


def test_api_key_not_in_generated(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("OPENCODE_CONFIG", str(tmp_path / "workspace/.opencode/opencode.json"))
    cfg, _, _ = build_opencode_config(Settings.from_env(), {"llm": {"provider": "openai", "model": "gpt-5.1", "api_key": "SECRET"}})
    assert "SECRET" not in json.dumps(cfg)


def test_write_main_agent_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    path = write_main_agent_prompt(Settings.from_env())
    text = path.read_text(encoding="utf-8")
    assert "This runtime is managed by EFP Portal." in text
    assert "Obey Portal capability/profile/policy metadata." in text
    assert "Do not write back to external systems unless explicitly allowed." in text
    assert "Use efp_* tools" in text
