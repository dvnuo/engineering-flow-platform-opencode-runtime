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


def test_empty_config_does_not_emit_external_json(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    env = build_runtime_env_from_config(s, {}).env
    assert "EFP_GITHUB_CONFIG_JSON" not in env
    assert "GITHUB_API_BASE_URL" not in env
    assert "EFP_JIRA_INSTANCES_JSON" not in env
    assert "EFP_CONFLUENCE_INSTANCES_JSON" not in env


def test_redacted_github_placeholder_not_in_json_or_env(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    env = build_runtime_env_from_config(s, {"github": {"enabled": True, "api_token": "***REDACTED***", "base_url": "https://ghe"}}).env
    text = json.dumps(env)
    assert "GITHUB_TOKEN" not in env
    assert "EFP_GITHUB_CONFIG_JSON" not in env
    assert "***REDACTED***" not in text


def test_redacted_atlassian_placeholder_not_in_json_or_env(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    cfg = {
        "jira": {"enabled": True, "instances": [{"url": "https://j", "username": "u", "token": "[redacted]"}]},
        "confluence": {"enabled": True, "instances": [{"url": "https://c/wiki", "username": "u", "token": "REDACTED"}]},
    }
    env = build_runtime_env_from_config(s, cfg).env
    text = json.dumps(env)
    assert "JIRA_BASE_URL" not in env
    assert "EFP_JIRA_INSTANCES_JSON" not in env
    assert "CONFLUENCE_BASE_URL" not in env
    assert "EFP_CONFLUENCE_INSTANCES_JSON" not in env
    assert "[redacted]" not in text
    assert "REDACTED" not in text


def test_atlassian_aliases_still_work(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    cfg = {
        "jira": {"enabled": True, "instances": [{"url": "https://j/", "email": "j@example.com", "api_token": "jt", "project_key": "PROJ"}]},
        "confluence": {"enabled": True, "instances": [{"url": "https://c/wiki/", "email": "c@example.com", "api_token": "ct", "space_key": "SPACE"}]},
    }
    env = build_runtime_env_from_config(s, cfg).env
    assert env["JIRA_EMAIL"] == "j@example.com" and env["JIRA_API_TOKEN"] == "jt" and env["JIRA_PROJECT_KEY"] == "PROJ"
    assert env["CONFLUENCE_EMAIL"] == "c@example.com" and env["CONFLUENCE_API_TOKEN"] == "ct" and env["CONFLUENCE_SPACE_KEY"] == "SPACE"
    jira_json = json.loads(env["EFP_JIRA_INSTANCES_JSON"])[0]
    conf_json = json.loads(env["EFP_CONFLUENCE_INSTANCES_JSON"])[0]
    assert jira_json == {"enabled": True, "url": "https://j", "token": "jt", "username": "j@example.com", "project": "PROJ", "api_version": "3"}
    assert conf_json == {"enabled": True, "url": "https://c/wiki", "token": "ct", "username": "c@example.com", "space": "SPACE"}


def test_uppercase_bracket_redacted_placeholder_not_exported(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    cfg = {
        "github": {"enabled": True, "api_token": "[REDACTED]", "base_url": "https://ghe"},
        "jira": {"enabled": True, "instances": [{"url": "https://j", "username": "u", "token": "[REDACTED]"}]},
        "confluence": {"enabled": True, "instances": [{"url": "https://c/wiki", "username": "u", "token": "[REDACTED]"}]},
    }
    env = build_runtime_env_from_config(s, cfg).env
    text = json.dumps(env)
    assert "GITHUB_TOKEN" not in env
    assert "EFP_GITHUB_CONFIG_JSON" not in env
    assert "JIRA_BASE_URL" not in env
    assert "EFP_JIRA_INSTANCES_JSON" not in env
    assert "CONFLUENCE_BASE_URL" not in env
    assert "EFP_CONFLUENCE_INSTANCES_JSON" not in env
    assert "[REDACTED]" not in text



def test_jira_username_password_exports_password_not_api_token(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    cfg = {
        "jira": {
            "enabled": True,
            "instances": [{"url": "https://jira.local", "username": "alice", "password": "pw", "project": "ENG"}],
        }
    }
    env = build_runtime_env_from_config(s, cfg).env
    assert env["JIRA_USERNAME"] == "alice"
    assert env["JIRA_PASSWORD"] == "pw"
    assert "JIRA_EMAIL" not in env
    assert "JIRA_API_TOKEN" not in env
    jira_json = json.loads(env["EFP_JIRA_INSTANCES_JSON"])[0]
    assert jira_json["username"] == "alice"
    assert jira_json["password"] == "pw"
    assert "token" not in jira_json
    assert jira_json["api_version"] == "2"


def test_jira_username_api_token_exports_email_api_token(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    cfg = {
        "jira": {
            "enabled": True,
            "instances": [{"url": "https://site.atlassian.net", "username": "alice@example.com", "api_token": "api-token", "project_key": "ENG"}],
        }
    }
    env = build_runtime_env_from_config(s, cfg).env
    assert env["JIRA_EMAIL"] == "alice@example.com"
    assert env["JIRA_API_TOKEN"] == "api-token"
    assert "JIRA_PASSWORD" not in env
    jira_json = json.loads(env["EFP_JIRA_INSTANCES_JSON"])[0]
    assert jira_json["token"] == "api-token"
    assert "password" not in jira_json
    assert jira_json["api_version"] == "3"


def test_confluence_username_password_exports_password_not_api_token(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    cfg = {
        "confluence": {
            "enabled": True,
            "instances": [{"url": "https://confluence.local", "username": "alice", "password": "pw", "space": "DOCS"}],
        }
    }
    env = build_runtime_env_from_config(s, cfg).env
    assert env["CONFLUENCE_USERNAME"] == "alice"
    assert env["CONFLUENCE_PASSWORD"] == "pw"
    assert "CONFLUENCE_EMAIL" not in env
    assert "CONFLUENCE_API_TOKEN" not in env
    conf_json = json.loads(env["EFP_CONFLUENCE_INSTANCES_JSON"])[0]
    assert conf_json["password"] == "pw"
    assert "token" not in conf_json

def test_strip_managed_external_env_removes_old_secret_but_keeps_path(monkeypatch):
    monkeypatch.setenv("JIRA_TOKEN", "old")
    monkeypatch.setenv("PATH", "/usr/bin")
    stripped = strip_managed_external_env(os.environ)
    assert "JIRA_TOKEN" not in stripped
    assert stripped["PATH"] == "/usr/bin"
