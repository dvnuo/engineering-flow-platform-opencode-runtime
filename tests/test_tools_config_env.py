"""build_cli_env projects jira/confluence/jenkins into the EFP_ env convention.

Locks the byte-identical naming/encoding the shared Go CLIs decode from the
RootConfig json tags: EFP_<PATH> uppercased, "_"-joined, "-"->"_", list index as
a path segment (e.g. EFP_JIRA_INSTANCES_0_BASE_URL).
"""

from __future__ import annotations

from efp_opencode_adapter.tools_config_env import (
    build_cli_env,
    build_tools_config_json,
    flatten_config_to_env,
)


SAMPLE = {
    "version": 1,
    "jira": {
        "enabled": True,
        "instances": [
            {"name": "jira-main", "url": "https://jira.example/", "username": "svc@example.com", "api_token": "jtok", "project": "QA", "api_version": "3"},
        ],
    },
    "confluence": {
        "enabled": True,
        "instances": [
            {"name": "docs", "url": "https://docs.example", "token": "ctok"},
        ],
    },
    "jenkins": {
        "enabled": True,
        "url": "https://ci.example/",
        "username": "ciuser",
        "password": "cipass",
    },
    "aws": {"enabled": True, "domain": "HBEU"},
    "mobile-auto": {"enabled": True, "default_provider": "browserstack"},
}


def test_build_cli_env_projects_jira_confluence_jenkins_instances():
    env = build_cli_env(SAMPLE)

    # Jira: username + api_token -> basic_api_key.
    assert env["EFP_JIRA_DEFAULT_INSTANCE"] == "jira-main"
    assert env["EFP_JIRA_INSTANCES_0_NAME"] == "jira-main"
    assert env["EFP_JIRA_INSTANCES_0_BASE_URL"] == "https://jira.example"
    assert env["EFP_JIRA_INSTANCES_0_API_VERSION"] == "3"
    assert env["EFP_JIRA_INSTANCES_0_REST_PATH"] == "/rest/api/3"
    assert env["EFP_JIRA_INSTANCES_0_AUTH_TYPE"] == "basic_api_key"
    assert env["EFP_JIRA_INSTANCES_0_AUTH_USERNAME"] == "svc@example.com"
    assert env["EFP_JIRA_INSTANCES_0_AUTH_API_KEY"] == "jtok"

    # Confluence: token only -> bearer_token.
    assert env["EFP_CONFLUENCE_DEFAULT_INSTANCE"] == "docs"
    assert env["EFP_CONFLUENCE_INSTANCES_0_BASE_URL"] == "https://docs.example"
    assert env["EFP_CONFLUENCE_INSTANCES_0_REST_PATH"] == "/rest/api"
    assert env["EFP_CONFLUENCE_INSTANCES_0_AUTH_TYPE"] == "bearer_token"
    assert env["EFP_CONFLUENCE_INSTANCES_0_AUTH_TOKEN"] == "ctok"

    # Jenkins: single flat block -> one instance; username + password.
    assert env["EFP_JENKINS_DEFAULT_INSTANCE"] == "jenkins"
    assert env["EFP_JENKINS_INSTANCES_0_NAME"] == "jenkins"
    assert env["EFP_JENKINS_INSTANCES_0_BASE_URL"] == "https://ci.example"
    assert env["EFP_JENKINS_INSTANCES_0_AUTH_TYPE"] == "basic_password"
    assert env["EFP_JENKINS_INSTANCES_0_AUTH_USERNAME"] == "ciuser"
    assert env["EFP_JENKINS_INSTANCES_0_AUTH_PASSWORD"] == "cipass"


def test_build_cli_env_drops_non_atlassian_jenkins_sections():
    env = build_cli_env(SAMPLE)
    # aws stays file-based, mobile keeps its own browserstack env, version is
    # metadata -- none of these belong in the CLI env dict.
    assert not any(key.startswith("EFP_AWS_") for key in env)
    assert not any(key.startswith("EFP_MOBILE") for key in env)
    assert "EFP_VERSION" not in env


def test_build_cli_env_empty_for_empty_or_disabled():
    assert build_cli_env({}) == {}
    assert build_cli_env(None) == {}
    assert build_cli_env({"jira": {"enabled": False, "instances": [{"url": "https://j", "token": "t"}]}}) == {}
    # jenkins with no base_url is dropped (CLI requires a base URL).
    assert build_cli_env({"jenkins": {"enabled": True, "username": "u", "password": "p"}}) == {}


def test_jira_username_password_defaults_to_api_version_2():
    env = build_cli_env({
        "jira": {"enabled": True, "instances": [{"url": "https://j", "username": "u", "password": "p"}]},
    })
    assert env["EFP_JIRA_INSTANCES_0_API_VERSION"] == "2"
    assert env["EFP_JIRA_INSTANCES_0_REST_PATH"] == "/rest/api/2"
    assert env["EFP_JIRA_INSTANCES_0_AUTH_TYPE"] == "basic_password"
    assert env["EFP_JIRA_INSTANCES_0_AUTH_PASSWORD"] == "p"


def test_flatten_encoding_rules():
    root = {
        "jira": {"instances": [{"base_url": "x", "empty": "", "missing": None, "flag": True, "n": 7}]},
    }
    out = flatten_config_to_env(root)
    assert out["EFP_JIRA_INSTANCES_0_BASE_URL"] == "x"
    assert out["EFP_JIRA_INSTANCES_0_FLAG"] == "true"
    assert out["EFP_JIRA_INSTANCES_0_N"] == "7"
    # None and empty string are omitted entirely.
    assert "EFP_JIRA_INSTANCES_0_EMPTY" not in out
    assert "EFP_JIRA_INSTANCES_0_MISSING" not in out


def test_build_tools_config_json_keeps_aws_mobile_version_verbatim():
    root = build_tools_config_json(SAMPLE)
    assert root["version"] == 1
    assert root["aws"] == {"enabled": True, "domain": "HBEU"}
    assert root["mobile-auto"] == {"enabled": True, "default_provider": "browserstack"}
    assert set(root) >= {"jira", "confluence", "jenkins", "aws", "mobile-auto", "version"}
