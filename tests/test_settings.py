from pathlib import Path

from efp_opencode_adapter.settings import Settings


def test_settings_default_state_dirs_are_root_home(monkeypatch):
    monkeypatch.delenv("EFP_ADAPTER_STATE_DIR", raising=False)
    monkeypatch.delenv("OPENCODE_DATA_DIR", raising=False)

    settings = Settings.from_env()

    assert settings.adapter_state_dir == Path("/root/.local/share/efp-compat")
    assert settings.opencode_data_dir == Path("/root/.local/share/opencode")
