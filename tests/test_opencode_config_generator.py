import json

from efp_opencode_adapter.opencode_config import EFP_WORKSPACE_INSTRUCTIONS_GLOB, build_opencode_config, model_from_runtime_profile, normalize_opencode_provider_id, provider_config_from_runtime_profile, write_opencode_config
from efp_opencode_adapter.settings import Settings


def test_build_opencode_config_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENCODE_CONFIG", str(tmp_path / "workspace/.opencode/opencode.json"))
    settings = Settings.from_env()
    cfg, _, updated = build_opencode_config(settings, None)
    assert cfg["autoupdate"] is False
    assert cfg["share"] == "disabled"
    assert cfg["server"] == {"hostname": "127.0.0.1", "port": 4096}
    assert "permission" in cfg and "efp-main" in cfg["agent"]
    assert cfg["instructions"] == [EFP_WORKSPACE_INSTRUCTIONS_GLOB]
    assert "model" not in cfg["agent"]["efp-main"]
    assert "prompt" not in cfg["agent"]["efp-main"]
    assert cfg["agent"]["efp-main"]["permission"] == {}
    assert "permission" in updated and "agent" in updated and "instructions" in updated


def test_model_mapping():
    assert model_from_runtime_profile({"llm": {"provider": "anthropic", "model": "claude-sonnet-4-5"}}) == "anthropic/claude-sonnet-4-5"
    assert model_from_runtime_profile({"llm": {"provider": "openai", "model": "gpt-5.1"}}) == "openai/gpt-5.1"
    assert model_from_runtime_profile({"llm": {"provider": "github_copilot", "model": "gpt-x"}}) == "github-copilot/gpt-x"
    assert model_from_runtime_profile({"llm": {"model": "github_copilot/gpt-x"}}) == "github-copilot/gpt-x"
    assert normalize_opencode_provider_id("github_copilot") == "github-copilot"


def test_permission_from_skills_index_does_not_restore_external_tools(tmp_path, monkeypatch):
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    state.mkdir(parents=True)
    (state / "skills-index.json").write_text(json.dumps({"skills": [{"opencode_name": "alpha"}]}))
    (state / "tools-index.json").write_text(json.dumps({"tools": [{"capability_id": "tool.read", "opencode_name": "efp_read", "policy_tags": ["read_only"]}, {"capability_id": "tool.update", "opencode_name": "efp_update", "policy_tags": ["mutation"]}]}))
    cfg, _, _ = build_opencode_config(Settings.from_env(), {"allowed_capability_ids": ["opencode.skill.alpha", "tool.read", "tool.update"]})
    perm = cfg["permission"]
    assert perm["skill"]["alpha"] == "allow"
    assert "efp_read" not in perm
    assert "efp_update" not in perm


def test_removed_tool_index_auto_allow_and_secret_not_leaked(tmp_path, monkeypatch):
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    state.mkdir(parents=True)
    (state / "tools-index.json").write_text(json.dumps({"tools": [{"capability_id": "tool.update", "opencode_name": "efp_update", "policy_tags": ["mutation"]}]}))
    cfg, _, _ = build_opencode_config(Settings.from_env(), {"allowed_capability_ids": ["tool.update"], "policy_context": {"allow_auto_run": True}, "llm": {"api_key": "SECRET"}})
    assert "efp_update" not in cfg["permission"]
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


def test_copilot_api_key_generates_local_proxy_base_url_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    source_token = "ghu_PORTAL_TOKEN"
    cfg, _, updated = build_opencode_config(
        Settings.from_env(),
        {"llm": {"provider": "github_copilot", "model": "gpt-x", "api_key": source_token}},
    )
    provider_cfg = cfg["provider"]["github-copilot"]
    assert provider_cfg["npm"] == "@ai-sdk/openai"
    assert provider_cfg["options"]["baseURL"] == "http://127.0.0.1:8000/api/internal/copilot"
    assert provider_cfg["options"]["apiKey"] == "efp-copilot-proxy"
    assert "provider" in updated
    encoded = json.dumps(cfg)
    assert source_token not in encoded
    assert "Authorization" not in encoded
    assert "ghu_" not in encoded
    assert "gho_" not in encoded
    assert "tid=" not in encoded


def test_copilot_timeout_ms_is_preserved_with_proxy_options(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    runtime_config = {
        "llm": {
            "provider": "github_copilot",
            "model": "gpt-x",
            "api_key": "ghu_PORTAL_TOKEN",
            "timeout_ms": 300000,
        }
    }
    settings = Settings.from_env()

    provider_patch = provider_config_from_runtime_profile(runtime_config, settings)
    provider_options = provider_patch["provider"]["github-copilot"]["options"]
    assert provider_options["baseURL"] == "http://127.0.0.1:8000/api/internal/copilot"
    assert provider_options["apiKey"] == "efp-copilot-proxy"
    assert provider_options["timeout"] == 300000

    cfg, _, updated = build_opencode_config(settings, runtime_config)
    assert cfg["agent"]["efp-main"]["model"] == "github-copilot/gpt-x"
    assert cfg["provider"]["github-copilot"]["npm"] == "@ai-sdk/openai"
    assert cfg["provider"]["github-copilot"]["options"]["baseURL"] == "http://127.0.0.1:8000/api/internal/copilot"
    assert cfg["provider"]["github-copilot"]["options"]["apiKey"] == "efp-copilot-proxy"
    assert cfg["provider"]["github-copilot"]["options"]["timeout"] == 300000
    assert "provider" in updated


def test_copilot_proxy_base_url_can_be_overridden(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_COPILOT_PROXY_BASE_URL", "http://127.0.0.1:9999/copilot/")
    cfg, _, _ = build_opencode_config(
        Settings.from_env(),
        {"llm": {"provider": "github_copilot", "model": "gpt-x", "oauth": {"access": "gho_PORTAL_TOKEN"}}},
    )
    provider_cfg = cfg["provider"]["github-copilot"]
    assert provider_cfg["npm"] == "@ai-sdk/openai"
    assert provider_cfg["options"]["baseURL"] == "http://127.0.0.1:9999/copilot"
    assert provider_cfg["options"]["apiKey"] == "efp-copilot-proxy"


def test_copilot_provider_base_url_keeps_provider_options_without_integration_header(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    cfg, _, updated = build_opencode_config(Settings.from_env(), {"llm": {"provider": "github_copilot", "model": "gpt-x", "api_base": "http://copilot.local"}})
    provider_cfg = cfg["provider"]["github-copilot"]
    assert provider_cfg["npm"] == "@ai-sdk/openai"
    assert provider_cfg["options"]["baseURL"] == "http://copilot.local"
    assert provider_cfg["options"]["apiKey"] == "efp-copilot-proxy"
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


def test_write_opencode_config_uses_workspace_instruction_glob_only(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENCODE_CONFIG", str(tmp_path / "workspace/.opencode/opencode.json"))
    settings = Settings.from_env()
    cfg, _, _ = build_opencode_config(settings, None)

    write_opencode_config(settings, cfg)

    written = json.loads(settings.opencode_config_path.read_text(encoding="utf-8"))
    assert written["instructions"] == [EFP_WORKSPACE_INSTRUCTIONS_GLOB]
    assert not (settings.workspace_dir / ".opencode" / "instructions").exists()
