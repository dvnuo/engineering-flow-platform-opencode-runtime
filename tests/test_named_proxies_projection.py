"""Where each connector's proxy lands in the opencode runtime: the child env, the
adapter's loopback proxies, the tools config, mobile-auto, inspect-image."""

from __future__ import annotations

from pathlib import Path

from efp_opencode_adapter import inspect_image_config as iic
from efp_opencode_adapter.mobile_cli_config import _build_mobile_config
from efp_opencode_adapter.outbound_proxy import outbound_proxy_config_for_url
from efp_opencode_adapter.runtime_env import build_runtime_env_from_config, strip_managed_external_env, write_runtime_env_file
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.tools_config_env import build_cli_env, build_tools_config_json

_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy", "EFP_LLM_PROXY", "EFP_LLM_NO_PROXY")


def _settings(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENCODE_CONFIG", str(tmp_path / "ws/.opencode/opencode.json"))
    for key in _PROXY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return Settings.from_env()


def proxy_section(**assignments):
    return {
        "enabled": True,
        "default": "corp-a",
        "proxies": [
            {"name": "corp-a", "url": "http://proxy-a.example.test:3128", "username": "ua", "password": "pa", "no_proxy": "db.internal"},
            {"name": "corp-b", "url": "https://proxy-b.example.test", "username": "ub", "password": "pb", "no_proxy": "chat.internal"},
        ],
        "assignments": assignments,
    }


def test_runtime_env_exports_the_default_proxy_and_the_model_provider_proxy(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    result = build_runtime_env_from_config(settings, {"proxy": proxy_section(llm="corp-b", jira="none")})
    env = result.env
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        assert env[key] == "http://ua:pa@proxy-a.example.test:3128"
    assert env["NO_PROXY"] == env["no_proxy"] == "db.internal,127.0.0.1,localhost"
    assert env["EFP_LLM_PROXY"] == "https://ub:pb@proxy-b.example.test"
    assert env["EFP_LLM_NO_PROXY"] == "chat.internal,127.0.0.1,localhost"
    assert env["EFP_PROXY_CORP_B_USERNAME"] == "ub" and env["EFP_PROXY_CORP_B_PASSWORD"] == "pb"
    assert env["EFP_PROXY_CORP_A_PASSWORD"] == "pa"
    assert "proxy" in result.updated_sections

    # The model provider on the default proxy, or without assignments, keeps
    # the environment path: no EFP_LLM_PROXY at all.
    env = build_runtime_env_from_config(settings, {"proxy": proxy_section(llm="corp-a")}).env
    assert "EFP_LLM_PROXY" not in env and "EFP_LLM_NO_PROXY" not in env
    legacy = build_runtime_env_from_config(settings, {"proxy": {"enabled": True, "url": "http://h:1", "username": "a", "password": "b"}}).env
    assert legacy["HTTPS_PROXY"] == "http://a:b@h:1"
    assert legacy["NO_PROXY"] == "127.0.0.1,localhost"
    assert "EFP_LLM_PROXY" not in legacy

    # none for the model provider is written so the loopback proxies connect directly.
    env = build_runtime_env_from_config(settings, {"proxy": proxy_section(llm="none")}).env
    assert env["EFP_LLM_PROXY"] == "none" and "EFP_LLM_NO_PROXY" not in env

    # An assignment to an unknown proxy is reported, not applied.
    result = build_runtime_env_from_config(settings, {"proxy": proxy_section(llm="ghost")})
    assert "EFP_LLM_PROXY" not in result.env
    assert any("ghost" in warning for warning in result.warnings)


def test_managed_env_strips_stale_llm_proxy_and_credentials(monkeypatch):
    stripped = strip_managed_external_env({"EFP_LLM_PROXY": "x", "EFP_LLM_NO_PROXY": "y", "EFP_PROXY_CORP_A_PASSWORD": "z", "KEEP": "1"})
    assert stripped == {"KEEP": "1"}


def test_outbound_proxy_honours_the_model_provider_proxy(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://process.proxy:8080")
    write_runtime_env_file(settings, {"HTTPS_PROXY": "http://default.proxy:8080", "EFP_LLM_PROXY": "https://ub:pb@proxy-b.example.test", "EFP_LLM_NO_PROXY": "chat.internal,127.0.0.1,localhost"})

    chosen = outbound_proxy_config_for_url(settings, "https://api.github.com/copilot_internal/v2/token")
    assert chosen.proxy_url == "https://ub:pb@proxy-b.example.test" and chosen.trust_env is False
    exempt = outbound_proxy_config_for_url(settings, "https://chat.internal/v1/api/v1/chat/completions")
    assert exempt.proxy_url is None and exempt.trust_env is False
    loopback = outbound_proxy_config_for_url(settings, "http://127.0.0.1:8000/x")
    assert loopback.proxy_url is None

    write_runtime_env_file(settings, {"HTTPS_PROXY": "http://default.proxy:8080", "EFP_LLM_PROXY": "none"})
    direct = outbound_proxy_config_for_url(settings, "https://api.github.com/copilot_internal/v2/token")
    assert direct.proxy_url is None and direct.trust_env is False

    write_runtime_env_file(settings, {"HTTPS_PROXY": "http://default.proxy:8080"})
    assert outbound_proxy_config_for_url(settings, "https://api.github.com/x").proxy_url == "http://default.proxy:8080"


def test_tools_config_materializes_the_assigned_proxies():
    effective = {
        "proxy": proxy_section(jira="corp-b", confluence="none", jenkins="corp-a"),
        "jira": {"enabled": True, "instances": [{"name": "main", "url": "https://jira.example.test", "token": "t"}]},
        "confluence": {"enabled": True, "instances": [{"name": "wiki", "url": "https://wiki.example.test", "token": "t"}, {"name": "own", "url": "https://own.example.test", "token": "t", "proxy": "corp-b"}]},
        "jenkins": {"enabled": True, "url": "https://ci.example.test/", "username": "u", "password": "p"},
    }
    root = build_tools_config_json(effective)
    assert root["jira"]["instances"][0]["proxy"] == "https://ub:pb@proxy-b.example.test"
    by_name = {row["name"]: row for row in root["confluence"]["instances"]}
    assert by_name["wiki"]["proxy"] == "none"
    assert by_name["own"]["proxy"] == "https://ub:pb@proxy-b.example.test"
    # The default proxy is the environment: no field.
    assert "proxy" not in root["jenkins"]["instances"][0]
    env = build_cli_env(effective)
    assert env["EFP_JIRA_INSTANCES_0_PROXY"] == "https://ub:pb@proxy-b.example.test"
    assert env["EFP_CONFLUENCE_INSTANCES_0_PROXY"] == "none"
    assert "EFP_JENKINS_INSTANCES_0_PROXY" not in env

    plain = build_tools_config_json({"proxy": {"enabled": True, "url": "http://h:1"}, "jira": effective["jira"]})
    assert "proxy" not in plain["jira"]["instances"][0]


def test_mobile_config_maps_the_browserstack_proxy(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    warnings: list[str] = []
    mobile, _ = _build_mobile_config(
        settings,
        {"proxy": proxy_section(browserstack="corp-b"), "mobile-auto": {"enabled": True, "browserstack": {"username": "bs", "access_key": "k", "http_proxy": {"force_proxy": True}}}},
        warnings,
    )
    browserstack = mobile["browserstack"]
    assert browserstack["http_proxy"] == {
        "force_proxy": True,
        "proxy_host": "https://proxy-b.example.test",
        "proxy_port": 443,
        "proxy_user_env": "EFP_PROXY_CORP_B_USERNAME",
        "proxy_pass_env": "EFP_PROXY_CORP_B_PASSWORD",
        "no_proxy_hosts": ["chat.internal"],
    }
    assert browserstack["local"]["proxy_host"] == "https://proxy-b.example.test"
    assert browserstack["local"]["proxy_pass_env"] == "EFP_PROXY_CORP_B_PASSWORD"
    assert browserstack["local"]["binary"]

    mobile, _ = _build_mobile_config(
        settings,
        {"proxy": proxy_section(browserstack="none"), "mobile-auto": {"enabled": True, "browserstack": {"username": "bs", "http_proxy": {"proxy_host": "old.proxy.test", "proxy_port": 1}}}},
        warnings,
    )
    assert mobile["browserstack"]["http_proxy"] == {"disable_proxy_discovery": True}
    assert mobile["browserstack"]["local"]["disable_proxy_discovery"] is True

    mobile, _ = _build_mobile_config(
        settings,
        {"proxy": proxy_section(), "mobile-auto": {"enabled": True, "browserstack": {"username": "bs", "http_proxy": {"proxy_host": "mine.proxy.test", "proxy_port": 2}}}},
        warnings,
    )
    assert mobile["browserstack"]["http_proxy"] == {"proxy_host": "mine.proxy.test", "proxy_port": 2}


def test_inspect_image_config_takes_the_model_provider_proxy():
    llm = {
        "provider": "github_copilot",
        "model": "gpt-5.6-terra",
        "api_key": "ghu",
        "vision": {"enabled": True, "model": "gpt-5.4"},
        "ai_platform": {
            "chat": {"host": "https://chat.int", "uri": "/v1/api/v1/chat/completions"},
            "ib2b": {"host": "https://ib2b.int", "uri": "/dsp/token"},
            "auth": {"username": "u", "password": "pw", "usercase": "uc"},
        },
    }
    settings, reason = iic.resolve_image_analysis_settings({"llm": llm, "proxy": proxy_section(llm="corp-b")})
    assert reason is None and settings.proxy == "https://ub:pb@proxy-b.example.test"
    assert iic.build_inspect_image_config(settings, token_file=Path("/tmp/token"))["inspect_image"]["api"] == {"proxy": "https://ub:pb@proxy-b.example.test"}
    # corp-b's no_proxy exempts chat.internal: a profile whose chat host is exempt connects directly.
    exempt_llm = dict(llm, ai_platform=dict(llm["ai_platform"], chat={"host": "https://chat.internal", "uri": "/v1"}))
    settings, _ = iic.resolve_image_analysis_settings({"llm": exempt_llm, "proxy": proxy_section(llm="corp-b")})
    assert settings.proxy == "none"
    settings, _ = iic.resolve_image_analysis_settings({"llm": llm, "proxy": proxy_section()})
    assert settings.proxy == ""
    assert "api" not in iic.build_inspect_image_config(settings, token_file=Path("/tmp/token"))["inspect_image"]
