from efp_opencode_adapter.outbound_proxy import outbound_proxy_for_url
from efp_opencode_adapter.runtime_env import write_runtime_env_file
from efp_opencode_adapter.settings import Settings


_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy")


def _settings(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENCODE_CONFIG", str(tmp_path / "ws/.opencode/opencode.json"))
    for key in _PROXY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return Settings.from_env()


def test_runtime_env_file_proxy_wins_over_process_env_proxy(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://process.proxy:8080")
    write_runtime_env_file(settings, {"HTTPS_PROXY": "http://runtime.proxy:8080"})

    assert outbound_proxy_for_url(settings, "https://api.github.com/copilot_internal/v2/token") == "http://runtime.proxy:8080"


def test_process_env_proxy_is_used_as_fallback(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://process.proxy:8080")

    assert outbound_proxy_for_url(settings, "https://api.github.com/copilot_internal/v2/token") == "http://process.proxy:8080"


def test_github_token_exchange_target_selects_https_proxy(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    write_runtime_env_file(settings, {"HTTPS_PROXY": "http://https.proxy:8080", "ALL_PROXY": "http://all.proxy:8080", "HTTP_PROXY": "http://http.proxy:8080"})

    assert outbound_proxy_for_url(settings, "https://api.github.com/copilot_internal/v2/token") == "http://https.proxy:8080"


def test_enterprise_copilot_api_target_selects_https_proxy(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    write_runtime_env_file(settings, {"HTTPS_PROXY": "http://https.proxy:8080", "HTTP_PROXY": "http://http.proxy:8080"})

    assert outbound_proxy_for_url(settings, "https://api.enterprise.githubcopilot.com/chat/completions") == "http://https.proxy:8080"


def test_no_proxy_exact_host_disables_github_token_exchange_proxy(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    write_runtime_env_file(settings, {"HTTPS_PROXY": "http://https.proxy:8080", "NO_PROXY": "api.github.com"})

    assert outbound_proxy_for_url(settings, "https://api.github.com/copilot_internal/v2/token") is None


def test_no_proxy_domain_suffix_disables_enterprise_copilot_proxy(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    write_runtime_env_file(settings, {"HTTPS_PROXY": "http://https.proxy:8080", "NO_PROXY": ".githubcopilot.com"})

    assert outbound_proxy_for_url(settings, "https://api.enterprise.githubcopilot.com/chat/completions") is None

    write_runtime_env_file(settings, {"HTTPS_PROXY": "http://https.proxy:8080", "NO_PROXY": "githubcopilot.com"})

    assert outbound_proxy_for_url(settings, "https://api.enterprise.githubcopilot.com/chat/completions") is None


def test_no_proxy_host_port_matches_default_target_port(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    write_runtime_env_file(settings, {"HTTPS_PROXY": "http://https.proxy:8080", "NO_PROXY": "api.github.com:443"})

    assert outbound_proxy_for_url(settings, "https://api.github.com/copilot_internal/v2/token") is None
