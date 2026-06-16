import json
import os
import subprocess
from pathlib import Path

import pytest

from efp_opencode_adapter.runtime_env import (
    build_runtime_env_from_config,
    ensure_opencode_xdg_data_home,
    opencode_xdg_data_home,
    read_runtime_env_file,
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


def _fake_adfs_assume(monkeypatch):
    calls = []

    def fake_run(args, text=False, capture_output=False, check=False, env=None):
        call = {
            "args": list(args),
            "text": text,
            "capture_output": capture_output,
            "check": check,
            "env": dict(env or {}),
        }
        calls.append(call)
        credentials_path = Path(call["env"]["AWS_SHARED_CREDENTIALS_FILE"])
        credentials_path.parent.mkdir(parents=True, exist_ok=True)
        credentials_path.write_text(
            "[default]\n"
            "generated = true\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("efp_opencode_adapter.runtime_env.subprocess.run", fake_run)
    return calls


def test_runtime_env_build_and_redact(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    adfs_calls = _fake_adfs_assume(monkeypatch)
    cfg = {
        "github": {"api_token": "t", "api_base_url": "https://api.github.com"},
        "jira": {"instances": [{"enabled": True, "url": "https://j/", "username": "u", "token": "x", "project": "P"}]},
        "confluence": {"instances": [{"enabled": True, "url": "https://c/", "token": "y"}]},
        "aws": {
            "enabled": True,
            "domain": "HBEU",
            "username": "aws-user",
            "password": "aws-password",
        },
        "proxy": {"enabled": True, "url": "http://h:1", "username": "a", "password": "b"},
        "git": {"author_name": "n", "author_email": "e@x"},
        "debug": {"enabled": True, "log_level": "DEBUG"},
    }
    r = build_runtime_env_from_config(s, cfg)
    assert r.env["JIRA_EMAIL"] == "u" and r.env["JIRA_API_TOKEN"] == "x" and "JIRA_TOKEN" not in r.env
    aws_credentials = Path(r.env["AWS_SHARED_CREDENTIALS_FILE"])
    assert "AWS_CONFIG_FILE" not in r.env
    assert aws_credentials.exists()
    assert "generated = true" in aws_credentials.read_text(encoding="utf-8")
    assert not list(aws_credentials.parent.glob("adfs-auth-*.json"))
    assert not (aws_credentials.parent / "aws-adfs-credential-process.py").exists()
    assume_args = adfs_calls[0]["args"]
    assert assume_args[0] == "adfs-assume"
    assert "--jenkins" in assume_args
    assert "-n" in assume_args
    assert assume_args[assume_args.index("-d") + 1] == "HBEU"
    assert assume_args[assume_args.index("-u") + 1] == "aws-user"
    assert "aws-password" not in " ".join(assume_args)
    assume_env = adfs_calls[0]["env"]
    assert assume_env["AD_PASS"] == "aws-password"
    assert assume_env["AWS_SHARED_CREDENTIALS_FILE"] == str(aws_credentials)
    assert "/opt/venv/bin" in assume_env["PATH"]
    assert ("/" + "app" + "/venv/bin") not in assume_env["PATH"]
    p = write_runtime_env_file(s, r.env)
    if os.name != "nt":
        assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    redacted = redact_env_for_status(r.env)
    assert redacted["GITHUB_TOKEN"] is True
    assert redacted["HTTPS_PROXY"] == "http://[redacted]@h:1"
    assert "aws-password" not in json.dumps(redacted)


def test_read_runtime_env_file_treats_permission_denied_path_as_missing(tmp_path, monkeypatch):
    denied_path = tmp_path / "opencode.env"
    original_exists = Path.exists

    def fake_exists(path):
        if path == denied_path:
            raise PermissionError("denied")
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", fake_exists)

    assert read_runtime_env_file(denied_path) == {}


def test_runtime_env_respects_disabled_external_sections(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    cfg = {
        "github": {"enabled": False, "api_token": "t"},
        "jira": {"enabled": False, "instances": [{"url": "https://j", "token": "x"}]},
        "confluence": {"enabled": False, "instances": [{"url": "https://c", "token": "y"}]},
        "aws": {"enabled": False, "domain": "HBEU", "username": "aws-user", "password": "aws-password"},
    }
    env = build_runtime_env_from_config(s, cfg).env
    for key in ("GITHUB_TOKEN", "EFP_GITHUB_CONFIG_JSON", "JIRA_BASE_URL", "EFP_JIRA_INSTANCES_JSON", "CONFLUENCE_BASE_URL", "EFP_CONFLUENCE_INSTANCES_JSON", "AWS_SHARED_CREDENTIALS_FILE"):
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
    assert "AWS_CONFIG_FILE" not in env


def test_runtime_env_aws_requires_all_portal_fields(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    env = build_runtime_env_from_config(s, {
        "aws": {
            "enabled": True,
            "domain": "HBEU",
            "username": "alice",
            "password": "***REDACTED***",
        }
    }).env
    assert "AWS_SHARED_CREDENTIALS_FILE" not in env


def test_runtime_env_aws_adfs_assume_failure_redacts_password(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    captured = {}

    def fake_run(args, text=False, capture_output=False, check=False, env=None):
        captured["args"] = list(args)
        captured["env"] = dict(env or {})
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="login failed for aws-password")

    monkeypatch.setattr("efp_opencode_adapter.runtime_env.subprocess.run", fake_run)

    with pytest.raises(RuntimeError) as exc:
        build_runtime_env_from_config(
            s,
            {
                "aws": {
                    "enabled": True,
                    "domain": "HBEU",
                    "username": "aws-user",
                    "password": "aws-password",
                }
            },
        )

    assert captured["args"][0] == "adfs-assume"
    assert captured["env"]["AD_PASS"] == "aws-password"
    assert "aws-password" not in str(exc.value)
    assert "[REDACTED_SECRET]" in str(exc.value)
    assert not (s.adapter_state_dir / "aws" / "config").exists()
    assert not (s.adapter_state_dir / "aws" / "credentials").exists()


def test_runtime_env_sets_java_maven_defaults(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    env = build_runtime_env_from_config(s, {}).env
    assert env["JAVA_HOME"] == "/opt/jdks/zulu21"
    assert env["JAVA21_HOME"] == "/opt/jdks/zulu21"
    assert env["JDK21_HOME"] == "/opt/jdks/zulu21"
    assert "JAVA8_HOME" not in env
    assert "JAVA17_HOME" not in env
    assert "JAVA25_HOME" not in env
    assert "JDK8_HOME" not in env
    assert "JDK17_HOME" not in env
    assert "JDK25_HOME" not in env
    assert env["MAVEN_HOME"] == "/opt/maven"
    assert env["MAVEN_CONFIG"] == "/root/.m2"
    assert env["MAVEN_SETTINGS_PATH"] == "/root/.m2/settings.xml"


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


def test_strip_managed_external_env_removes_old_jdk_env_but_keeps_path(monkeypatch):
    monkeypatch.setenv("JAVA8_HOME", "/bad/zulu8")
    monkeypatch.setenv("JAVA17_HOME", "/bad/zulu17")
    monkeypatch.setenv("JAVA25_HOME", "/bad/zulu25")
    monkeypatch.setenv("JDK8_HOME", "/bad/zulu8")
    monkeypatch.setenv("JDK17_HOME", "/bad/zulu17")
    monkeypatch.setenv("JDK25_HOME", "/bad/zulu25")
    monkeypatch.setenv("PATH", "/usr/bin")
    stripped = strip_managed_external_env(os.environ)
    for key in ("JAVA8_HOME", "JAVA17_HOME", "JAVA25_HOME", "JDK8_HOME", "JDK17_HOME", "JDK25_HOME"):
        assert key not in stripped
    assert stripped["PATH"] == "/usr/bin"


def test_runtime_env_sets_disable_claude_prompt_default(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    env = build_runtime_env_from_config(s, {}).env
    assert env["OPENCODE_DISABLE_CLAUDE_CODE_PROMPT"] == "1"


def test_runtime_env_maps_opencode_xdg_data_home_to_configured_data_dir(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    env = build_runtime_env_from_config(s, {}).env
    xdg_home = opencode_xdg_data_home(s)
    opencode_link = xdg_home / "opencode"

    assert env["OPENCODE_DATA_DIR"] == str(s.opencode_data_dir)
    assert env["XDG_DATA_HOME"] == str(xdg_home)
    assert opencode_link.exists()
    assert opencode_link.resolve() == s.opencode_data_dir.resolve()


def test_ensure_opencode_xdg_data_home_rejects_conflicting_regular_path(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(tmp_path / "opencode-data"))
    s = _settings(tmp_path, monkeypatch)
    xdg_home = opencode_xdg_data_home(s)
    xdg_home.mkdir(parents=True)
    (xdg_home / "opencode").write_text("not a directory", encoding="utf-8")

    try:
        ensure_opencode_xdg_data_home(s)
    except RuntimeError as exc:
        assert "OpenCode XDG data path conflict" in str(exc)
    else:
        raise AssertionError("expected conflicting OpenCode XDG data path to fail")
