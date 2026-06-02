from pathlib import Path

from efp_opencode_adapter.settings import Settings


def test_settings_default_state_dirs_are_root_home(monkeypatch):
    monkeypatch.delenv("EFP_ADAPTER_STATE_DIR", raising=False)
    monkeypatch.delenv("OPENCODE_DATA_DIR", raising=False)

    settings = Settings.from_env()

    assert settings.adapter_state_dir == Path("/root/.local/share/efp-compat")
    assert settings.opencode_data_dir == Path("/root/.local/share/opencode")


def test_settings_permission_defaults(monkeypatch):
    monkeypatch.delenv("EFP_OPENCODE_PERMISSION_MODE", raising=False)
    monkeypatch.delenv("EFP_OPENCODE_ALLOW_BASH_ALL", raising=False)
    monkeypatch.delenv("EFP_COPILOT_PROXY_BASE_URL", raising=False)
    monkeypatch.delenv("EFP_COPILOT_GITHUB_API_BASE_URL", raising=False)
    monkeypatch.delenv("EFP_COPILOT_API_BASE_URL", raising=False)
    settings = Settings.from_env()
    assert settings.opencode_permission_mode == "workspace_full_access"
    assert settings.opencode_allow_bash_all is True
    assert settings.copilot_proxy_base_url == "http://127.0.0.1:8000/api/internal/copilot"
    assert settings.copilot_github_api_base_url == "https://api.github.com"
    assert settings.copilot_api_base_url == "https://api.enterprise.githubcopilot.com"


def test_settings_permission_overrides(monkeypatch):
    monkeypatch.setenv("EFP_OPENCODE_PERMISSION_MODE", "profile_policy")
    monkeypatch.setenv("EFP_OPENCODE_ALLOW_BASH_ALL", "false")
    monkeypatch.setenv("EFP_COPILOT_PROXY_BASE_URL", "http://127.0.0.1:9000/copilot/")
    monkeypatch.setenv("EFP_COPILOT_GITHUB_API_BASE_URL", "http://github-api.local/")
    monkeypatch.setenv("EFP_COPILOT_API_BASE_URL", "https://copilot-api.local/")
    settings = Settings.from_env()
    assert settings.opencode_permission_mode == "profile_policy"
    assert settings.opencode_allow_bash_all is False
    assert settings.copilot_proxy_base_url == "http://127.0.0.1:9000/copilot"
    assert settings.copilot_github_api_base_url == "http://github-api.local"
    assert settings.copilot_api_base_url == "https://copilot-api.local"


def test_settings_permission_mode_unknown_or_empty_fallback(monkeypatch):
    monkeypatch.setenv("EFP_OPENCODE_PERMISSION_MODE", "")
    assert Settings.from_env().opencode_permission_mode == "workspace_full_access"
    monkeypatch.setenv("EFP_OPENCODE_PERMISSION_MODE", "unknown")
    assert Settings.from_env().opencode_permission_mode == "workspace_full_access"
    monkeypatch.setenv("EFP_OPENCODE_PERMISSION_MODE", "profile-policy")
    assert Settings.from_env().opencode_permission_mode == "profile_policy"
    monkeypatch.setenv("EFP_OPENCODE_PERMISSION_MODE", "restricted")
    assert Settings.from_env().opencode_permission_mode == "profile_policy"


def test_settings_legacy_external_tool_env_ignored(monkeypatch):
    monkeypatch.setenv("EFP_TOOLS_DIR", "/tmp/legacy-tools")
    monkeypatch.setenv("OPENCODE_TOOLS_DIR", "/tmp/legacy-op-tools")
    monkeypatch.setenv("DEFAULT_TOOL_REPO_URL", "https://example.com/legacy.git")
    monkeypatch.setenv("DEFAULT_TOOL_BRANCH", "legacy")
    monkeypatch.setenv("TOOL_REPO_URL", "https://example.com/runtime.git")
    monkeypatch.setenv("TOOL_BRANCH", "runtime")
    settings = Settings.from_env()
    assert settings.skills_dir == Path("/app/skills")
    assert not hasattr(settings, "tools_dir")


def test_settings_no_long_chat_recovery_options(monkeypatch, tmp_path):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path))
    settings = Settings.from_env()

    forbidden_attrs = [
        "chat_total_wall_timeout_seconds",
        "chat_auto_continue_enabled",
        "chat_auto_continue_max_turns",
        "chat_auto_continue_checkpoint_enabled",
        "chat_auto_continue_checkpoint_prompt",
        "chat_auto_continue_prompt",
        "chat_auto_continue_no_progress_stop",
        "chat_auto_continue_after_running_timeout",
        "chat_no_progress_timeout_seconds",
    ]

    for name in forbidden_attrs:
        assert not hasattr(settings, name)


def test_chat_short_request_and_event_replay_settings_env_overrides(monkeypatch):
    monkeypatch.setenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "42")
    monkeypatch.setenv("EFP_CHAT_COMPLETION_POLL_SECONDS", "0.25")
    monkeypatch.setenv("EFP_CHAT_SUBMIT_TIMEOUT_SECONDS", "301")
    monkeypatch.setenv("EFP_EVENT_REPLAY_LIMIT", "12")
    monkeypatch.setenv("EFP_EVENT_REPLAY_TTL_SECONDS", "13")

    settings = Settings.from_env()

    assert settings.chat_completion_timeout_seconds == 42
    assert settings.chat_completion_poll_seconds == 0.25
    assert settings.chat_submit_timeout_seconds == 301
    assert settings.event_replay_limit == 12
    assert settings.event_replay_ttl_seconds == 13


def test_chat_submit_timeout_env_is_floored_at_300(monkeypatch):
    monkeypatch.setenv("EFP_CHAT_SUBMIT_TIMEOUT_SECONDS", "60")

    settings = Settings.from_env()

    assert settings.chat_submit_timeout_seconds == 300
