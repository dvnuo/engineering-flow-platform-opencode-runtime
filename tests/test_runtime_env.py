import json
import os

from efp_opencode_adapter.runtime_env import (
    build_runtime_env_from_config,
    redact_env_for_status,
    strip_managed_external_env,
    write_runtime_env_file,
)
from efp_opencode_adapter.settings import Settings


def _settings(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENCODE_CONFIG", str(tmp_path / "ws/.opencode/opencode.json"))
    return Settings.from_env()


def test_runtime_env_build_and_redact(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    cfg = {
        "github": {"api_token": "t", "api_base_url": "https://api.github.com"},
        "jira": {"instances": [{"enabled": True, "url": "https://j/", "username": "u", "token": "x", "project": "P"}]},
        "confluence": {"instances": [{"enabled": True, "url": "https://c/", "token": "y"}]},
        "proxy": {"enabled": True, "url": "http://h:1", "username": "a", "password": "b"},
        "git": {"author_name": "n", "author_email": "e@x"},
        "debug": {"enabled": True, "log_level": "DEBUG"},
    }
    r = build_runtime_env_from_config(s, cfg)
    assert r.env["JIRA_EMAIL"] == "u" and r.env["JIRA_API_TOKEN"] == "x" and "JIRA_TOKEN" not in r.env
    p = write_runtime_env_file(s, r.env)
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    assert redact_env_for_status(r.env)["GITHUB_TOKEN"] is True


def test_runtime_env_respects_disabled_external_sections(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    cfg = {
        "github": {"enabled": False, "api_token": "t"},
        "jira": {"enabled": False, "instances": [{"url": "https://j", "token": "x"}]},
        "confluence": {"enabled": False, "instances": [{"url": "https://c", "token": "y"}]},
    }
    env = build_runtime_env_from_config(s, cfg).env
    for key in ("GITHUB_TOKEN", "EFP_GITHUB_CONFIG_JSON", "JIRA_BASE_URL", "EFP_JIRA_INSTANCES_JSON", "CONFLUENCE_BASE_URL", "EFP_CONFLUENCE_INSTANCES_JSON"):
        assert key not in env


def test_runtime_env_supports_portal_git_user_shape(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    env = build_runtime_env_from_config(s, {"git": {"user": {"name": "Alice", "email": "a@example.com"}}}).env
    assert env["GIT_AUTHOR_NAME"] == "Alice"
    assert env["GIT_AUTHOR_EMAIL"] == "a@example.com"


def test_runtime_env_github_base_url_alias_and_api_token_aliases(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    env = build_runtime_env_from_config(s, {"github": {"enabled": True, "base_url": "https://ghe.example/api/v3/", "api_token": "ghx"}}).env
    assert env["GITHUB_API_BASE_URL"] == "https://ghe.example/api/v3"
    cfg_json = json.loads(env["EFP_GITHUB_CONFIG_JSON"])
    assert cfg_json["base_url"] == "https://ghe.example/api/v3"
    assert cfg_json["api_base_url"] == "https://ghe.example/api/v3"


def test_runtime_env_does_not_export_redacted_placeholders(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    cfg = {"github": {"enabled": True, "api_token": "***REDACTED***"}, "jira": {"instances": [{"url": "https://j", "token": "[redacted]"}]}}
    env = build_runtime_env_from_config(s, cfg).env
    assert "GITHUB_TOKEN" not in env
    assert "JIRA_TOKEN" not in env and "JIRA_API_TOKEN" not in env


def test_strip_managed_external_env_removes_old_secret_but_keeps_path(monkeypatch):
    monkeypatch.setenv("JIRA_TOKEN", "old")
    monkeypatch.setenv("PATH", "/usr/bin")
    stripped = strip_managed_external_env(os.environ)
    assert "JIRA_TOKEN" not in stripped
    assert stripped["PATH"] == "/usr/bin"
