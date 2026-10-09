"""The Proxy connector resolved into a choice per connector (efp_opencode_adapter/proxy_plan.py)."""

from __future__ import annotations

from efp_opencode_adapter.proxy_plan import (
    KIND_ENVIRONMENT,
    KIND_NONE,
    KIND_PROXY,
    SOURCE_ASSIGNMENT,
    SOURCE_DEFAULT,
    SOURCE_DISABLED,
    SOURCE_ENVIRONMENT,
    SOURCE_INSTANCE,
    SOURCE_NO_PROXY,
    SOURCE_UNKNOWN,
    build_proxy_plan,
    hostname_of,
    no_proxy_exempts,
    normalize_proxy_section,
)


def list_section(**overrides):
    section = {
        "enabled": True,
        "default": "corp-a",
        "proxies": [
            {
                "name": "corp-a",
                "url": "http://proxy-a.example.test:3128",
                "username": "ua",
                "password": "p:a",
                "no_proxy": "localhost,.svc.cluster.local,db.internal",
            },
            {"name": "corp-b", "url": "https://proxy-b.example.test"},
        ],
        "assignments": {
            "llm": "corp-b",
            "pgsql": "corp-b",
            "jira": "",
            "github": "corp-a",
            "splunk": "none",
            "nexus": "ghost",
        },
    }
    section.update(overrides)
    return section


def test_legacy_section_reads_as_one_default_proxy():
    legacy = {"enabled": True, "url": "http://proxy.example.test:8080", "username": "u", "password": "p", "noProxy": "a.internal"}
    section = normalize_proxy_section(legacy)
    assert section["default"] == "default"
    assert section["proxies"] == [
        {"name": "default", "url": "http://proxy.example.test:8080", "username": "u", "password": "p", "no_proxy": "a.internal"}
    ]
    plan = build_proxy_plan(legacy)
    assert plan.configured and plan.default.name == "default"
    assert plan.environment()["HTTPS_PROXY"] == "http://u:p@proxy.example.test:8080"
    # Loopback is always exempt: the opencode child must reach the adapter's own proxies.
    assert plan.environment()["NO_PROXY"] == "a.internal,127.0.0.1,localhost"
    for connector in ("llm", "jira", "browserstack"):
        assert plan.choice(connector).kind == KIND_ENVIRONMENT


def test_redacted_placeholders_read_as_no_credentials():
    plan = build_proxy_plan({"enabled": True, "url": "http://proxy.example.test:8080", "username": "u", "password": "***REDACTED***"})
    assert plan.default.password == ""
    assert plan.environment()["HTTPS_PROXY"] == "http://u@proxy.example.test:8080"


def test_environment_carries_the_default_proxy_only():
    plan = build_proxy_plan(list_section())
    env = plan.environment()
    expected = "http://ua:p%3Aa@proxy-a.example.test:3128"
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        assert env[key] == expected
    assert env["NO_PROXY"] == env["no_proxy"] == "localhost,.svc.cluster.local,db.internal,127.0.0.1"
    assert len(env) == 8
    assert "proxy-b" not in "".join(env.values())
    assert build_proxy_plan(list_section(enabled=False)).environment() == {}
    assert build_proxy_plan(None).environment() == {}
    assert build_proxy_plan({"enabled": True, "proxies": [{"name": "a", "url": "http://a.example.test:1"}]}).environment()["NO_PROXY"] == "127.0.0.1,localhost"


def test_choice_per_connector():
    plan = build_proxy_plan(list_section())
    jira = plan.choice("jira", host="jira.example.test")
    assert (jira.kind, jira.source, jira.setting) == (KIND_ENVIRONMENT, SOURCE_ENVIRONMENT, "")
    splunk = plan.choice("splunk")
    assert (splunk.kind, splunk.source, splunk.setting) == (KIND_NONE, SOURCE_ASSIGNMENT, "none")
    llm = plan.choice("llm", host="chat.example.test")
    assert (llm.kind, llm.source, llm.setting) == (KIND_PROXY, SOURCE_ASSIGNMENT, "https://proxy-b.example.test")
    github = plan.choice("github", host="github.example.test")
    assert (github.kind, github.source, github.setting) == (KIND_ENVIRONMENT, SOURCE_DEFAULT, "")
    nexus = plan.choice("nexus")
    assert (nexus.kind, nexus.source) == (KIND_ENVIRONMENT, SOURCE_UNKNOWN)
    assert any("ghost" in warning for warning in plan.warnings)
    exempt = plan.choice("pgsql", host="db.internal", instance_setting="corp-a")
    assert (exempt.kind, exempt.source, exempt.setting) == (KIND_NONE, SOURCE_NO_PROXY, "none")
    assert plan.choice("pgsql", host="db.example.test", instance_setting="none").source == SOURCE_INSTANCE
    own = plan.choice("pgsql", host="db.example.test", instance_setting="http://own.proxy.test:9")
    assert (own.kind, own.source, own.setting) == (KIND_PROXY, SOURCE_INSTANCE, "http://own.proxy.test:9")
    assert plan.choice("pgsql", instance_setting="corp-b").setting == "https://proxy-b.example.test"
    disabled = build_proxy_plan(list_section(enabled=False)).choice("llm")
    assert (disabled.kind, disabled.source) == (KIND_ENVIRONMENT, SOURCE_DISABLED)
    # A row's own none or URL stands on its own, connector switched off or
    # not; a name needs the connector's list.
    off = build_proxy_plan(list_section(enabled=False))
    assert off.choice("pgsql", instance_setting="none").setting == "none"
    assert off.choice("pgsql", instance_setting="http://own.proxy.test:9").setting == "http://own.proxy.test:9"
    assert off.choice("pgsql", instance_setting="corp-b").kind == KIND_ENVIRONMENT


def test_credential_environment_and_summary_keep_secrets_apart():
    plan = build_proxy_plan(list_section())
    assert plan.credential_environment() == {"EFP_PROXY_CORP_A_USERNAME": "ua", "EFP_PROXY_CORP_A_PASSWORD": "p:a"}
    summary = plan.summary()
    assert summary["default"] == "corp-a"
    assert summary["proxies"] == [
        {"name": "corp-a", "address": "proxy-a.example.test:3128"},
        {"name": "corp-b", "address": "proxy-b.example.test:443"},
    ]
    assert "p:a" not in str(summary) and "p%3Aa" not in str(summary)
    assert plan.choice("llm").describe() == {"kind": "proxy", "source": "assignment", "proxy": "corp-b", "address": "proxy-b.example.test:443"}


def test_no_proxy_exempts_and_hostname_of():
    rules = "localhost,.svc.cluster.local, db.internal ,*.corp.test,https://nexus.example.test:8081"
    assert no_proxy_exempts("db.internal", rules)
    assert no_proxy_exempts("api.svc.cluster.local", rules)
    assert no_proxy_exempts("a.corp.test", rules)
    assert no_proxy_exempts("nexus.example.test", rules)
    assert no_proxy_exempts("127.0.0.1", "")
    assert not no_proxy_exempts("notdb.internal", rules)
    assert not no_proxy_exempts("jira.example.test", rules)
    assert hostname_of("https://Jira.Example.test:8443/rest") == "jira.example.test"
    assert hostname_of("db.internal:5432") == "db.internal"
    assert hostname_of("") == ""
