from pathlib import Path

from efp_opencode_adapter.runtime_env import build_runtime_env_from_config
from efp_opencode_adapter.settings import Settings


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        opencode_url="http://127.0.0.1:4096",
        adapter_state_dir=tmp_path / "state",
        workspace_dir=tmp_path / "workspace",
        skills_dir=tmp_path / "skills",
        workspace_repos_dir=tmp_path / "workspace" / "repos",
        git_checkout_timeout_seconds=120,
        opencode_data_dir=tmp_path / "opencode-data",
        opencode_config_path=tmp_path / "workspace" / ".opencode" / "opencode.json",
        opencode_version="1.14.39",
        ready_timeout_seconds=1,
    )


def test_runtime_env_github_git_mapping(tmp_path):
    settings = make_settings(tmp_path)
    result = build_runtime_env_from_config(settings, {"github": {"enabled": True, "username": "efp-bot", "token": "github_pat_test", "api_base_url": "https://api.github.com"}, "git": {"user": {"name": "EFP Bot", "email": "efp@example.com"}}})
    env = result.env
    assert env["GH_TOKEN"] == "github_pat_test"
    assert env["GITHUB_TOKEN"] == "github_pat_test"
    assert env["GIT_PASSWORD"] == "github_pat_test"
    assert env["GIT_USERNAME"] == "efp-bot"
    assert env["GH_HOST"] == "github.com"
    assert env["GIT_ASKPASS"].endswith("git-askpass.sh")
    assert env["GIT_CONFIG_GLOBAL"].endswith("gitconfig")
    assert env["GIT_AUTHOR_NAME"] == "EFP Bot"
    assert env["GIT_AUTHOR_EMAIL"] == "efp@example.com"
    assert "github" in result.updated_sections and "git" in result.updated_sections


def test_runtime_env_fallback_from_process_env(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setenv("GH_TOKEN", "env_token")
    monkeypatch.setenv("GITHUB_USERNAME", "env-user")
    result = build_runtime_env_from_config(settings, {})
    assert result.env["GH_TOKEN"] == "env_token"
    assert result.env["GIT_USERNAME"] == "env-user"


def test_runtime_env_redacted_token_not_emitted(tmp_path):
    settings = make_settings(tmp_path)
    result = build_runtime_env_from_config(settings, {"github": {"enabled": True, "token": "***redacted***"}})
    assert "GH_TOKEN" not in result.env
    assert "GITHUB_TOKEN" not in result.env


def test_runtime_env_git_author_fallback(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setenv("GITHUB_USERNAME", "fallback-user")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "fallback@example.com")
    result = build_runtime_env_from_config(settings, {"github": {"enabled": True, "token": "t"}})
    assert result.env["GIT_AUTHOR_NAME"] == "fallback-user"
    assert result.env["GIT_AUTHOR_EMAIL"] == "fallback@example.com"
