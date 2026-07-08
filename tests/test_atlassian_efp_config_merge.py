"""Atlassian config is merged into the shared EFP config file (EFP_CONFIG).

Previously jira/confluence were written to a separate JSON under
ATLASSIAN_CONFIG, which EFP_CONFIG outranks -- so the CLI resolved the EFP
config (mobile-auto/aws only) and never saw confluence. These lock the fix:
jira/confluence now co-locate with the other sections in efp_config_path.
"""

from __future__ import annotations

import yaml

from efp_opencode_adapter.atlassian_cli_config import write_atlassian_cli_config
from efp_opencode_adapter.settings import Settings


def _settings(tmp_path, monkeypatch, cfg_path):
    monkeypatch.setenv("EFP_CONFIG", str(cfg_path))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("ATLASSIAN_CONFIG", raising=False)
    settings = Settings.from_env()
    assert settings.efp_config_path == cfg_path
    return settings


def test_atlassian_merges_into_efp_config_and_preserves_other_sections(tmp_path, monkeypatch):
    cfg_path = tmp_path / "home" / ".efp" / "config.yaml"
    cfg_path.parent.mkdir(parents=True)
    # Pre-existing mobile-auto section (as write_mobile_cli_config would leave).
    cfg_path.write_text("mobile-auto:\n  default_provider: browserstack\n", encoding="utf-8")
    settings = _settings(tmp_path, monkeypatch, cfg_path)

    result = write_atlassian_cli_config(
        settings,
        {
            "confluence": {
                "enabled": True,
                "instances": [{"name": "docs", "url": "https://docs.example", "token": "secret"}],
            }
        },
    )

    # Written into the shared EFP config file, not a separate JSON.
    assert result.path == str(cfg_path)
    assert result.env == {"ATLASSIAN_CONFIG": str(cfg_path)}
    assert not settings.atlassian_config_path.exists()

    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert "confluence" in data  # confluence now present where EFP_CONFIG points
    assert data["confluence"]["instances"]
    assert data["mobile-auto"]["default_provider"] == "browserstack"  # preserved


def test_atlassian_disabled_removes_only_its_sections(tmp_path, monkeypatch):
    cfg_path = tmp_path / ".efp" / "config.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(
        "mobile-auto:\n  default_provider: browserstack\nconfluence:\n  instances: []\n",
        encoding="utf-8",
    )
    settings = _settings(tmp_path, monkeypatch, cfg_path)

    write_atlassian_cli_config(settings, {"confluence": {"enabled": False}})

    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert "confluence" not in data  # removed
    assert data["mobile-auto"]["default_provider"] == "browserstack"  # kept
