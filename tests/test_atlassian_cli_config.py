import json

import pytest

from efp_opencode_adapter.atlassian_cli_config import build_atlassian_cli_config


def test_jira_username_password_becomes_basic_password():
    cfg, result = build_atlassian_cli_config({
        "jira": {"enabled": True, "instances": [{"name": "jira-main", "url": "https://jira.example/", "username": "svc", "password": "pw", "project": "QA"}]},
    })

    instance = cfg["jira"]["instances"][0]
    assert result.configured is True
    assert instance["base_url"] == "https://jira.example"
    assert instance["rest_path"] == "/rest/api/2"
    assert instance["api_version"] == "2"
    assert instance["auth"] == {"type": "basic_password", "username": "svc", "password": "pw"}
    assert instance["default_project"] == "QA"


@pytest.mark.parametrize("key", ["token", "api_token", "api_key"])
def test_username_token_variants_become_basic_api_key(key):
    cfg, _ = build_atlassian_cli_config({
        "jira": {"enabled": True, "instances": [{"name": "jira-main", "base_url": "https://jira.example", "username": "svc", key: "secret"}]},
    })

    assert cfg["jira"]["instances"][0]["auth"] == {"type": "basic_api_key", "username": "svc", "api_key": "secret"}


def test_token_only_becomes_bearer_token():
    cfg, _ = build_atlassian_cli_config({
        "confluence": {"enabled": True, "instances": [{"name": "docs", "url": "https://docs.example", "token": "bearer"}]},
    })

    instance = cfg["confluence"]["instances"][0]
    assert instance["rest_path"] == "/rest/api"
    assert instance["auth"] == {"type": "bearer_token", "token": "bearer"}


def test_disabled_instances_are_skipped_and_default_falls_back():
    cfg, result = build_atlassian_cli_config({
        "jira": {
            "enabled": True,
            "default_instance": "disabled",
            "instances": [
                {"name": "disabled", "url": "https://disabled.example", "token": "secret", "enabled": False},
                {"name": "active", "url": "https://active.example", "token": "secret"},
            ],
        },
    })

    assert result.jira_instances == 1
    assert cfg["jira"]["default_instance"] == "active"
    assert [item["name"] for item in cfg["jira"]["instances"]] == ["active"]


def test_redacted_secret_strings_are_ignored():
    cfg, result = build_atlassian_cli_config({
        "jira": {"enabled": True, "instances": [{"name": "jira-main", "url": "https://jira.example", "username": "svc", "password": "***REDACTED***"}]},
    })

    assert cfg == {"version": 1}
    assert result.configured is False
    assert result.jira_instances == 0


def test_build_atlassian_cli_config_writes_no_file_and_returns_empty_env(tmp_path):
    # jira/confluence are no longer written to any config file; the builder only
    # derives reporting status now, so it must not touch the filesystem.
    _, result = build_atlassian_cli_config({
        "jira": {"enabled": True, "instances": [{"name": "jira-main", "url": "https://jira.example", "token": "secret"}]},
    })

    assert result.configured is True
    assert result.path == ""
    assert result.env == {}
    assert not list(tmp_path.iterdir())


def test_redacted_status_never_contains_secret_values():
    _, result = build_atlassian_cli_config({
        "jira": {"enabled": True, "instances": [{"name": "jira-main", "url": "https://jira.example", "username": "svc", "password": "pw-secret"}]},
        "confluence": {"enabled": True, "instances": [{"name": "docs", "url": "https://docs.example", "token": "token-secret"}]},
    })

    encoded = json.dumps(result.redacted_status)
    assert "pw-secret" not in encoded
    assert "token-secret" not in encoded
    assert "password_present" in encoded
    assert "token_present" in encoded
