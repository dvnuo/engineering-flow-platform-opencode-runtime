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
        opencode_data_dir=tmp_path / "state" / "xdg-data" / "opencode",
        opencode_config_path=tmp_path / "workspace" / ".opencode" / "opencode.json",
        efp_config_path=tmp_path / "workspace" / ".efp" / "config.yaml",
        mobile_state_dir=tmp_path / "workspace" / ".efp" / "mobile-auto" / "runs",
        mobile_artifacts_dir=tmp_path / "workspace" / ".efp" / "mobile-auto" / "artifacts",
        browserstack_local_binary_path=Path("/usr/local/bin/BrowserStackLocal"),
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


def test_runtime_env_ignores_ambient_process_env_tokens(tmp_path, monkeypatch):
    # The profile env blob is the sole config source: ambient tokens must not
    # leak back into the managed runtime env.
    settings = make_settings(tmp_path)
    monkeypatch.setenv("GH_TOKEN", "env_token")
    monkeypatch.setenv("GITHUB_TOKEN", "env_token")
    monkeypatch.setenv("EFP_GITHUB_TOKEN", "env_token")
    monkeypatch.setenv("GITHUB_USERNAME", "env-user")
    result = build_runtime_env_from_config(settings, {})
    assert "GH_TOKEN" not in result.env
    assert "GITHUB_TOKEN" not in result.env
    assert "GIT_USERNAME" not in result.env
    assert "github" not in result.updated_sections


def test_runtime_env_redacted_token_not_emitted(tmp_path):
    settings = make_settings(tmp_path)
    result = build_runtime_env_from_config(settings, {"github": {"enabled": True, "token": "***redacted***"}})
    assert "GH_TOKEN" not in result.env
    assert "GITHUB_TOKEN" not in result.env


def test_runtime_env_git_author_ignores_ambient_env(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setenv("GITHUB_USERNAME", "fallback-user")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "ambient-name")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "fallback@example.com")
    monkeypatch.setenv("GITHUB_EMAIL", "ambient@example.com")
    result = build_runtime_env_from_config(settings, {"github": {"enabled": True, "token": "t"}})
    # Author name falls back to the config-derived github username default.
    assert result.env["GIT_AUTHOR_NAME"] == "x-access-token"
    assert "GIT_AUTHOR_EMAIL" not in result.env


def test_runtime_env_ignores_ambient_gh_host_and_tokens(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setenv("GH_TOKEN", "env_token")
    monkeypatch.setenv("GH_HOST", "ghe.example.com")

    env = build_runtime_env_from_config(settings, {}).env

    assert "GH_TOKEN" not in env
    assert "GH_HOST" not in env
    assert "GH_ENTERPRISE_TOKEN" not in env
    assert "GITHUB_ENTERPRISE_TOKEN" not in env


def test_runtime_env_config_host_beats_env_gh_host(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setenv("GH_HOST", "env.example.com")

    result = build_runtime_env_from_config(settings, {
        "github": {
            "enabled": True,
            "token": "t",
            "host": "portal.example.com",
            "api_base_url": "https://api.portal.example.com"
        }
    })

    assert result.env["GH_HOST"] == "portal.example.com"


def test_runtime_env_sets_git_config_even_without_token(tmp_path):
    settings = make_settings(tmp_path)
    env = build_runtime_env_from_config(settings, {}).env
    assert env["GIT_CONFIG_GLOBAL"].endswith("gitconfig")
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_ASKPASS"].endswith("git-askpass.sh")
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_EDITOR"] == "true"
