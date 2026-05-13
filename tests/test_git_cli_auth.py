from pathlib import Path

import subprocess

from efp_opencode_adapter.git_cli_auth import write_git_gh_auth_assets
from efp_opencode_adapter.settings import Settings
from tests.test_runtime_env_git_gh import make_settings


def test_write_assets_with_token(tmp_path):
    settings = make_settings(tmp_path)
    env = {"GH_TOKEN": "github_pat_test", "GIT_USERNAME": "efp-bot", "GH_HOST": "github.com", "GIT_AUTHOR_NAME": "EFP Bot", "GIT_AUTHOR_EMAIL": "efp@example.com"}
    result = write_git_gh_auth_assets(settings, env)
    assert result["configured"] is True
    assert Path(result["askpass_path"]).exists()
    assert Path(result["gitconfig_path"]).exists()
    assert Path(result["credential_store_path"]).exists()
    assert "github_pat_test" in Path(result["credential_store_path"]).read_text()
    assert "insteadOf = git@github.com:" in Path(result["gitconfig_path"]).read_text()
    assert Path(result["askpass_path"]).stat().st_mode & 0o111


def test_write_assets_without_token(tmp_path):
    settings = make_settings(tmp_path)
    result = write_git_gh_auth_assets(settings, {})
    assert result["configured"] is True
    assert result["credential_configured"] is False
    assert result["reason"] == "missing_token_public_git_ok"
    assert Path(result["gitconfig_path"]).exists()
    assert Path(result["askpass_path"]).exists()
    assert not Path(result["credential_store_path"]).exists()


def test_generated_gitconfig_is_parseable_by_git(tmp_path):
    settings = make_settings(tmp_path)
    result = write_git_gh_auth_assets(settings, {"GH_HOST": "github.com"})
    proc = subprocess.run(["git", "config", "--file", result["gitconfig_path"], "--list"], capture_output=True, text=True)
    assert proc.returncode == 0


def test_git_config_values_are_sanitized(tmp_path):
    settings = make_settings(tmp_path)
    result = write_git_gh_auth_assets(settings, {
        "GIT_AUTHOR_NAME": "bad\nname",
        "GIT_AUTHOR_EMAIL": "bad\r\nemail@example.com",
    })
    text = Path(result["gitconfig_path"]).read_text()
    assert "bad\nname" not in text
    proc = subprocess.run(["git", "config", "--file", result["gitconfig_path"], "--list"], capture_output=True, text=True)
    assert proc.returncode == 0
