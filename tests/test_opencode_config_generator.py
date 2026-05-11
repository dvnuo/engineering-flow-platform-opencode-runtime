import json

from efp_opencode_adapter.opencode_config import build_opencode_config, model_from_runtime_profile, normalize_opencode_provider_id
from efp_opencode_adapter.settings import Settings


def test_build_opencode_config_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENCODE_CONFIG", str(tmp_path / "workspace/.opencode/opencode.json"))
    cfg, _, updated = build_opencode_config(Settings.from_env(), None)
    assert cfg["autoupdate"] is False
    assert cfg["share"] == "disabled"
    assert cfg["server"] == {"hostname": "127.0.0.1", "port": 4096}
    assert "permission" in cfg and "efp-main" in cfg["agent"]
    assert "model" not in cfg["agent"]["efp-main"]
    assert "prompt" not in cfg["agent"]["efp-main"]
    assert cfg["agent"]["efp-main"]["permission"] == {}
    assert "permission" in updated and "agent" in updated


def test_model_mapping():
    assert model_from_runtime_profile({"llm": {"provider": "anthropic", "model": "claude-sonnet-4-5"}}) == "anthropic/claude-sonnet-4-5"
    assert model_from_runtime_profile({"llm": {"provider": "openai", "model": "gpt-5.1"}}) == "openai/gpt-5.1"
    assert model_from_runtime_profile({"llm": {"provider": "github_copilot", "model": "gpt-x"}}) == "github-copilot/gpt-x"
    assert model_from_runtime_profile({"llm": {"model": "github_copilot/gpt-x"}}) == "github-copilot/gpt-x"
    assert normalize_opencode_provider_id("github_copilot") == "github-copilot"


def test_permission_from_indexes(tmp_path, monkeypatch):
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    state.mkdir(parents=True)
    (state / "skills-index.json").write_text(json.dumps({"skills": [{"opencode_name": "alpha"}]}))
    (state / "tools-index.json").write_text(json.dumps({"tools": [{"capability_id": "tool.read", "opencode_name": "efp_read", "policy_tags": ["read_only"]}, {"capability_id": "tool.update", "opencode_name": "efp_update", "policy_tags": ["mutation"]}]}))
    cfg, _, _ = build_opencode_config(Settings.from_env(), {"allowed_capability_ids": ["opencode.skill.alpha", "tool.read", "tool.update"]})
    perm = cfg["permission"]
    assert perm["skill"]["alpha"] == "allow"
    assert perm["efp_read"] == "allow"
    assert perm["efp_update"] == "allow"


def test_permission_auto_allow_and_secret_not_leaked(tmp_path, monkeypatch):
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    state.mkdir(parents=True)
    (state / "tools-index.json").write_text(json.dumps({"tools": [{"capability_id": "tool.update", "opencode_name": "efp_update", "policy_tags": ["mutation"]}]}))
    cfg, _, _ = build_opencode_config(Settings.from_env(), {"allowed_capability_ids": ["tool.update"], "policy_context": {"allow_auto_run": True}, "llm": {"api_key": "SECRET"}})
    assert cfg["permission"]["efp_update"] == "allow"
    assert "SECRET" not in json.dumps(cfg)


def test_provider_base_url_mapping(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    cfg, _, _ = build_opencode_config(Settings.from_env(), {"llm": {"provider": "openai", "model": "gpt-4.1", "base_url": "http://litellm.local/v1", "timeout_ms": 300000}})
    assert cfg["provider"]["openai"]["options"]["baseURL"] == "http://litellm.local/v1"
    assert cfg["provider"]["openai"]["options"]["timeout"] == 300000
    assert "api_key" not in json.dumps(cfg).lower()


def test_copilot_provider_config_does_not_include_integration_header(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    cfg, _, updated = build_opencode_config(Settings.from_env(), {"llm": {"provider": "github_copilot", "model": "gpt-x"}})
    assert cfg["agent"]["efp-main"]["model"] == "github-copilot/gpt-x"
    assert "copilot-integration-id" not in json.dumps(cfg)
    assert "provider" not in cfg
    assert "provider" not in updated


def test_copilot_provider_base_url_keeps_provider_options_without_integration_header(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    cfg, _, updated = build_opencode_config(Settings.from_env(), {"llm": {"provider": "github_copilot", "model": "gpt-x", "api_base": "http://copilot.local"}})
    assert cfg["provider"]["github-copilot"]["options"]["baseURL"] == "http://copilot.local"
    assert "copilot-integration-id" not in json.dumps(cfg)
    assert "provider" in updated


def test_non_copilot_providers_do_not_include_integration_header(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    cfg_openai, _, _ = build_opencode_config(Settings.from_env(), {"llm": {"provider": "openai", "model": "gpt-5"}})
    cfg_anthropic, _, _ = build_opencode_config(Settings.from_env(), {"llm": {"provider": "anthropic", "model": "claude-sonnet-4-5"}})
    assert "copilot-integration-id" not in json.dumps(cfg_openai)
    assert "copilot-integration-id" not in json.dumps(cfg_anthropic)


def test_agent_permission_not_empty_object(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    cfg, _, _ = build_opencode_config(Settings.from_env(), None)
    assert cfg["permission"]["edit"] == "allow"
    assert cfg["permission"]["write"] == "allow"
    assert cfg["permission"]["bash"] == {"*": "allow"}
    assert cfg["agent"]["efp-main"].get("permission") == {}


def test_config_hash_changes_with_permission_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_OPENCODE_PERMISSION_MODE", "workspace_full_access")
    cfg1, h1, _ = build_opencode_config(Settings.from_env(), None)
    monkeypatch.setenv("EFP_OPENCODE_PERMISSION_MODE", "profile_policy")
    monkeypatch.setenv("EFP_OPENCODE_ALLOW_BASH_ALL", "false")
    cfg2, h2, _ = build_opencode_config(Settings.from_env(), None)
    assert h1 != h2
    assert cfg1["permission"]["*"] != cfg2["permission"]["*"]


def test_config_hash_deterministic(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    s = Settings.from_env()
    _, h1, _ = build_opencode_config(s, {"llm": {"provider": "openai", "model": "gpt-5"}})
    _, h2, _ = build_opencode_config(s, {"llm": {"provider": "openai", "model": "gpt-5"}})
    assert h1 == h2
