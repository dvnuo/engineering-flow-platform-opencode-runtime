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


def _fake_aws_auth(monkeypatch):
    calls = []

    def fake_run(args, input=None, text=False, capture_output=False, check=False, env=None, timeout=None):
        call = {
            "args": list(args),
            "input": input,
            "text": text,
            "capture_output": capture_output,
            "check": check,
            "env": dict(env or {}),
            "timeout": timeout,
        }
        calls.append(call)
        if call["args"][:3] == ["aws-auth", "auth", "login"]:
            config_path = Path(call["env"]["EFP_CONFIG"])
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                "version: 1\n"
                "aws:\n"
                "  enabled: true\n"
                "  domain: HBEU\n"
                "  username: aws-user\n"
                "  password: aws-password\n",
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("efp_opencode_adapter.runtime_env.subprocess.run", fake_run)
    return calls


def test_runtime_env_build_and_redact(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    aws_auth_calls = _fake_aws_auth(monkeypatch)
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
        "jenkins": {
            "enabled": True,
            "url": "https://ci.example/",
            "username": "jenkins-user",
            "password": "jenkins-password",
        },
        "mobile-auto": {
            "enabled": True,
            "browserstack": {
                "username": "bs-user",
                "access_key": "bs-access-key",
                "username_env": "BROWSERSTACK_USERNAME",
                "access_key_env": "BROWSERSTACK_ACCESS_KEY",
            },
        },
        "proxy": {"enabled": True, "url": "http://h:1", "username": "a", "password": "b"},
        "git": {"author_name": "n", "author_email": "e@x"},
        "debug": {"enabled": True, "log_level": "DEBUG"},
    }
    r = build_runtime_env_from_config(s, cfg)
    # jira/confluence/jenkins now flow through the EFP_-prefixed convention, not
    # the old flat JIRA_*/CONFLUENCE_* vars.
    assert r.env["EFP_JIRA_INSTANCES_0_BASE_URL"] == "https://j"
    assert r.env["EFP_JIRA_INSTANCES_0_AUTH_USERNAME"] == "u"
    assert r.env["EFP_JIRA_INSTANCES_0_AUTH_API_KEY"] == "x"
    assert "JIRA_EMAIL" not in r.env and "JIRA_API_TOKEN" not in r.env
    aws_credentials = Path(r.env["AWS_SHARED_CREDENTIALS_FILE"])
    efp_config = Path(r.env["EFP_CONFIG"])
    assert "AWS_CONFIG_FILE" not in r.env
    assert aws_credentials.parent.exists()
    assert not aws_credentials.exists()
    assert not list(aws_credentials.parent.glob("adfs-auth-*.json"))
    assert not (aws_credentials.parent / "aws-adfs-credential-process.py").exists()
    assert efp_config.exists()
    configure_args = aws_auth_calls[0]["args"]
    assert configure_args == [
        "aws-auth",
        "auth",
        "login",
        "--domain",
        "HBEU",
        "--username",
        "aws-user",
        "--password-stdin",
        "--json",
    ]
    assert aws_auth_calls[0]["input"] == "aws-password\n"
    assert aws_auth_calls[0]["timeout"]
    assert "aws-password" not in " ".join(configure_args)
    assert len(aws_auth_calls) == 1
    configure_env = aws_auth_calls[0]["env"]
    assert "AD_PASS" not in configure_env
    assert configure_env["AWS_SHARED_CREDENTIALS_FILE"] == str(aws_credentials)
    assert configure_env["EFP_CONFIG"] == str(efp_config)
    efp_config_text = efp_config.read_text(encoding="utf-8")
    assert "HBEU" in efp_config_text
    assert "aws-user" in efp_config_text
    assert "aws-password" in efp_config_text
    assert "jenkins:" not in efp_config_text
    assert r.env["EFP_JENKINS_INSTANCES_0_BASE_URL"] == "https://ci.example"
    assert r.env["EFP_JENKINS_INSTANCES_0_AUTH_USERNAME"] == "jenkins-user"
    assert r.env["EFP_JENKINS_INSTANCES_0_AUTH_PASSWORD"] == "jenkins-password"
    assert "EFP_JENKINS_USERNAME" not in r.env and "JENKINS_USERNAME" not in r.env
    assert r.env["BROWSERSTACK_USERNAME"] == "bs-user"
    assert r.env["BROWSERSTACK_ACCESS_KEY"] == "bs-access-key"
    assert r.env["MOBILE_AUTO_STATE_DIR"] == str(s.mobile_state_dir)
    assert r.env["MOBILE_AUTO_ARTIFACTS_DIR"] == str(s.mobile_artifacts_dir)
    assert r.env["BROWSERSTACK_LOCAL_BINARY"] == s.browserstack_local_binary_path.as_posix()
    assert "/opt/venv/bin" in configure_env["PATH"]
    assert ("/" + "app" + "/venv/bin") not in configure_env["PATH"]
    p = write_runtime_env_file(s, r.env)
    if os.name != "nt":
        assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    redacted = redact_env_for_status(r.env)
    assert redacted["GITHUB_TOKEN"] is True
    assert redacted["EFP_JENKINS_INSTANCES_0_AUTH_PASSWORD"] is True
    assert redacted["BROWSERSTACK_ACCESS_KEY"] is True
    assert redacted["HTTPS_PROXY"] == "http://[redacted]@h:1"
    assert "aws-password" not in json.dumps(redacted)
    assert "jenkins-password" not in json.dumps(redacted)


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
        "jenkins": {"enabled": False, "username": "u", "password": "p"},
    }
    env = build_runtime_env_from_config(s, cfg).env
    for key in ("GITHUB_TOKEN", "AWS_SHARED_CREDENTIALS_FILE", "BROWSERSTACK_USERNAME", "BROWSERSTACK_ACCESS_KEY"):
        assert key not in env
    # No EFP_ jira/confluence/jenkins vars are emitted for disabled sections.
    assert not any(key.startswith(("EFP_JIRA_", "EFP_CONFLUENCE_", "EFP_JENKINS_")) for key in env)
    assert env["EFP_CONFIG"] == str(s.efp_config_path)
    assert env["MOBILE_AUTO_STATE_DIR"] == str(s.mobile_state_dir)


def test_runtime_env_supports_portal_git_user_shape(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    env = build_runtime_env_from_config(s, {"git": {"user": {"name": "Alice", "email": "a@example.com"}}}).env
    assert env["GIT_AUTHOR_NAME"] == "Alice"
    assert env["GIT_AUTHOR_EMAIL"] == "a@example.com"


def test_runtime_env_github_base_url_alias_and_api_token_aliases(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    env = build_runtime_env_from_config(s, {"github": {"enabled": True, "base_url": "https://ghe.example/api/v3/", "api_token": "ghx"}}).env
    assert env["GITHUB_API_BASE_URL"] == "https://ghe.example/api/v3"
    assert env["GH_HOST"] == "ghe.example"
    # EFP_GITHUB_CONFIG_JSON was a dead var never read by any CLI; it is gone.
    assert "EFP_GITHUB_CONFIG_JSON" not in env


def test_runtime_env_github_does_not_export_redacted_placeholder(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    env = build_runtime_env_from_config(s, {"github": {"enabled": True, "api_token": "***REDACTED***"}}).env
    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env


def test_empty_config_does_not_emit_external_json(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    env = build_runtime_env_from_config(s, {}).env
    assert "EFP_GITHUB_CONFIG_JSON" not in env
    assert "GITHUB_API_BASE_URL" not in env
    assert not any(key.startswith(("EFP_JIRA_", "EFP_CONFLUENCE_", "EFP_JENKINS_")) for key in env)
    assert env["EFP_CONFIG"] == str(s.efp_config_path)
    assert env["MOBILE_AUTO_STATE_DIR"] == str(s.mobile_state_dir)
    assert env["MOBILE_AUTO_ARTIFACTS_DIR"] == str(s.mobile_artifacts_dir)
    assert env["BROWSERSTACK_LOCAL_BINARY"] == s.browserstack_local_binary_path.as_posix()
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
    assert env["EFP_CONFIG"] == str(s.efp_config_path)
    assert "AWS_SHARED_CREDENTIALS_FILE" not in env


def test_runtime_env_jenkins_projects_efp_instance(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    result = build_runtime_env_from_config(
        s,
        {"jenkins": {"enabled": True, "url": "https://ci.local/", "username": "alice", "password": "pw"}},
    )
    assert result.env["EFP_JENKINS_DEFAULT_INSTANCE"] == "jenkins"
    assert result.env["EFP_JENKINS_INSTANCES_0_BASE_URL"] == "https://ci.local"
    assert result.env["EFP_JENKINS_INSTANCES_0_AUTH_TYPE"] == "basic_password"
    assert result.env["EFP_JENKINS_INSTANCES_0_AUTH_USERNAME"] == "alice"
    assert result.env["EFP_JENKINS_INSTANCES_0_AUTH_PASSWORD"] == "pw"
    assert "jenkins" in result.updated_sections


def test_runtime_env_jenkins_without_base_url_is_dropped(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    result = build_runtime_env_from_config(
        s,
        {"jenkins": {"enabled": True, "username": "alice", "password": "pw"}},
    )
    assert result.env["EFP_CONFIG"] == str(s.efp_config_path)
    assert not any(key.startswith("EFP_JENKINS_") for key in result.env)


def test_runtime_env_aws_auth_failure_redacts_password(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    captured = {}

    def fake_run(args, input=None, text=False, capture_output=False, check=False, env=None, timeout=None):
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

    assert captured["args"][:3] == ["aws-auth", "auth", "login"]
    assert "AD_PASS" not in captured["env"]
    assert "aws-password" not in str(exc.value)
    assert "[REDACTED_SECRET]" in str(exc.value)
    assert not (s.adapter_state_dir / "aws" / "config").exists()
    assert not (s.adapter_state_dir / "aws" / "credentials").exists()
    assert not (s.adapter_state_dir / "efp" / "config.yaml").exists()


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


def test_atlassian_username_api_token_projects_basic_api_key(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    cfg = {
        "jira": {"enabled": True, "instances": [{"name": "j", "url": "https://j/", "username": "j@example.com", "api_token": "jt", "api_version": "3"}]},
        "confluence": {"enabled": True, "instances": [{"name": "c", "url": "https://c/wiki/", "username": "c@example.com", "api_token": "ct"}]},
    }
    env = build_runtime_env_from_config(s, cfg).env
    assert env["EFP_JIRA_INSTANCES_0_BASE_URL"] == "https://j"
    assert env["EFP_JIRA_INSTANCES_0_API_VERSION"] == "3"
    assert env["EFP_JIRA_INSTANCES_0_AUTH_TYPE"] == "basic_api_key"
    assert env["EFP_JIRA_INSTANCES_0_AUTH_USERNAME"] == "j@example.com"
    assert env["EFP_JIRA_INSTANCES_0_AUTH_API_KEY"] == "jt"
    assert env["EFP_CONFLUENCE_INSTANCES_0_BASE_URL"] == "https://c/wiki"
    assert env["EFP_CONFLUENCE_INSTANCES_0_AUTH_TYPE"] == "basic_api_key"
    assert env["EFP_CONFLUENCE_INSTANCES_0_AUTH_API_KEY"] == "ct"
    # The former flat JIRA_*/CONFLUENCE_* + EFP_*_INSTANCES_JSON vars are gone.
    for key in ("JIRA_EMAIL", "JIRA_API_TOKEN", "EFP_JIRA_INSTANCES_JSON", "CONFLUENCE_EMAIL", "CONFLUENCE_API_TOKEN", "EFP_CONFLUENCE_INSTANCES_JSON"):
        assert key not in env


def test_jira_username_password_projects_basic_password_and_default_api_version(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    cfg = {
        "jira": {"enabled": True, "instances": [{"url": "https://jira.local", "username": "alice", "password": "pw", "project": "ENG"}]},
    }
    env = build_runtime_env_from_config(s, cfg).env
    assert env["EFP_JIRA_INSTANCES_0_AUTH_TYPE"] == "basic_password"
    assert env["EFP_JIRA_INSTANCES_0_AUTH_USERNAME"] == "alice"
    assert env["EFP_JIRA_INSTANCES_0_AUTH_PASSWORD"] == "pw"
    # Native defaults api_version to "2" unless explicitly "3".
    assert env["EFP_JIRA_INSTANCES_0_API_VERSION"] == "2"
    assert env["EFP_JIRA_INSTANCES_0_REST_PATH"] == "/rest/api/2"
    assert "JIRA_USERNAME" not in env and "JIRA_PASSWORD" not in env


def test_confluence_username_password_projects_basic_password(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    cfg = {
        "confluence": {"enabled": True, "instances": [{"url": "https://confluence.local", "username": "alice", "password": "pw", "space": "DOCS"}]},
    }
    env = build_runtime_env_from_config(s, cfg).env
    assert env["EFP_CONFLUENCE_INSTANCES_0_AUTH_TYPE"] == "basic_password"
    assert env["EFP_CONFLUENCE_INSTANCES_0_AUTH_USERNAME"] == "alice"
    assert env["EFP_CONFLUENCE_INSTANCES_0_AUTH_PASSWORD"] == "pw"
    assert "CONFLUENCE_USERNAME" not in env and "CONFLUENCE_PASSWORD" not in env


def test_strip_managed_external_env_removes_old_secret_but_keeps_path(monkeypatch):
    monkeypatch.setenv("JIRA_TOKEN", "old")
    monkeypatch.setenv("PATH", "/usr/bin")
    stripped = strip_managed_external_env(os.environ)
    assert "JIRA_TOKEN" not in stripped
    assert stripped["PATH"] == "/usr/bin"


def test_strip_managed_external_env_removes_efp_convention_prefixes(monkeypatch):
    monkeypatch.setenv("EFP_JIRA_INSTANCES_0_AUTH_TOKEN", "stale")
    monkeypatch.setenv("EFP_CONFLUENCE_INSTANCES_0_BASE_URL", "https://stale")
    monkeypatch.setenv("EFP_JENKINS_DEFAULT_INSTANCE", "stale")
    monkeypatch.setenv("PATH", "/usr/bin")
    stripped = strip_managed_external_env(os.environ)
    for key in ("EFP_JIRA_INSTANCES_0_AUTH_TOKEN", "EFP_CONFLUENCE_INSTANCES_0_BASE_URL", "EFP_JENKINS_DEFAULT_INSTANCE"):
        assert key not in stripped
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
