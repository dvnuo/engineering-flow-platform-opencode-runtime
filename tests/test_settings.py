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
