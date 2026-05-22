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
    settings = Settings.from_env()
    assert settings.opencode_permission_mode == "workspace_full_access"
    assert settings.opencode_allow_bash_all is True


def test_settings_permission_overrides(monkeypatch):
    monkeypatch.setenv("EFP_OPENCODE_PERMISSION_MODE", "profile_policy")
    monkeypatch.setenv("EFP_OPENCODE_ALLOW_BASH_ALL", "false")
    settings = Settings.from_env()
    assert settings.opencode_permission_mode == "profile_policy"
    assert settings.opencode_allow_bash_all is False


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


def test_deprecated_chat_long_run_settings_defaults_disabled(monkeypatch):
    for name in [
        "EFP_CHAT_TOTAL_WALL_TIMEOUT_SECONDS",
        "EFP_CHAT_TIMEOUT_RECOVERY_ENABLED",
        "EFP_CHAT_TIMEOUT_RECOVERY_MAX_SECONDS",
        "EFP_CHAT_TIMEOUT_RECOVERY_POLL_SECONDS",
        "EFP_CHAT_AUTO_CONTINUE_CHECKPOINT_ENABLED",
        "EFP_CHAT_AUTO_CONTINUE_CHECKPOINT_PROMPT",
        "EFP_CHAT_AUTO_CONTINUE_PROMPT",
        "EFP_CHAT_AUTO_CONTINUE_ENABLED",
        "EFP_CHAT_AUTO_CONTINUE_MAX_TURNS",
        "EFP_CHAT_NO_PROGRESS_TIMEOUT_SECONDS",
        "EFP_EVENT_REPLAY_LIMIT",
        "EFP_EVENT_REPLAY_TTL_SECONDS",
    ]:
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_env()

    assert settings.chat_total_wall_timeout_seconds == 21600
    assert settings.chat_timeout_recovery_enabled is False
    assert settings.chat_timeout_recovery_max_seconds == 900
    assert settings.chat_timeout_recovery_poll_seconds == 2.0
    assert settings.chat_auto_continue_checkpoint_enabled is True
    assert settings.chat_auto_continue_enabled is False
    assert settings.chat_auto_continue_max_turns == 0
    assert "Continue the same user request" in settings.chat_auto_continue_checkpoint_prompt
    assert settings.chat_auto_continue_prompt == settings.chat_auto_continue_checkpoint_prompt
    assert settings.chat_no_progress_timeout_seconds == 1800
    assert settings.event_replay_limit == 500
    assert settings.event_replay_ttl_seconds == 21600


def test_chat_long_run_settings_env_overrides(monkeypatch):
    monkeypatch.setenv("EFP_CHAT_TOTAL_WALL_TIMEOUT_SECONDS", "42")
    monkeypatch.setenv("EFP_CHAT_TIMEOUT_RECOVERY_ENABLED", "false")
    monkeypatch.setenv("EFP_CHAT_TIMEOUT_RECOVERY_MAX_SECONDS", "7")
    monkeypatch.setenv("EFP_CHAT_TIMEOUT_RECOVERY_POLL_SECONDS", "0.25")
    monkeypatch.setenv("EFP_CHAT_AUTO_CONTINUE_MAX_TURNS", "50")
    monkeypatch.setenv("EFP_CHAT_AUTO_CONTINUE_CHECKPOINT_ENABLED", "false")
    monkeypatch.setenv("EFP_CHAT_AUTO_CONTINUE_CHECKPOINT_PROMPT", "checkpoint prompt")
    monkeypatch.setenv("EFP_CHAT_NO_PROGRESS_TIMEOUT_SECONDS", "9")
    monkeypatch.setenv("EFP_EVENT_REPLAY_LIMIT", "12")
    monkeypatch.setenv("EFP_EVENT_REPLAY_TTL_SECONDS", "13")

    settings = Settings.from_env()

    assert settings.chat_total_wall_timeout_seconds == 42
    assert settings.chat_timeout_recovery_enabled is False
    assert settings.chat_timeout_recovery_max_seconds == 7
    assert settings.chat_timeout_recovery_poll_seconds == 0.25
    assert settings.chat_auto_continue_max_turns == 50
    assert settings.chat_auto_continue_checkpoint_enabled is False
    assert settings.chat_auto_continue_checkpoint_prompt == "checkpoint prompt"
    assert settings.chat_auto_continue_prompt == "checkpoint prompt"
    assert settings.chat_no_progress_timeout_seconds == 9
    assert settings.event_replay_limit == 12
    assert settings.event_replay_ttl_seconds == 13


def test_legacy_auto_continue_prompt_env_still_supported(monkeypatch):
    monkeypatch.delenv("EFP_CHAT_AUTO_CONTINUE_CHECKPOINT_PROMPT", raising=False)
    monkeypatch.setenv("EFP_CHAT_AUTO_CONTINUE_PROMPT", "legacy prompt")

    settings = Settings.from_env()

    assert settings.chat_auto_continue_checkpoint_prompt == "legacy prompt"
    assert settings.chat_auto_continue_prompt == "legacy prompt"
