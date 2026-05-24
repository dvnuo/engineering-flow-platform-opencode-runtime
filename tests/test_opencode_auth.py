from efp_opencode_adapter.opencode_auth import build_opencode_auth_from_llm


def test_copilot_gho_token_is_not_opencode_auth():
    result = build_opencode_auth_from_llm({"provider": "github_copilot", "api_key": "  gho_TEST  "})
    assert result.provider == "github-copilot"
    assert result.auth_type == "copilot_plugin_proxy"
    assert result.auth_info is None
    assert result.warning is None


def test_copilot_ghu_token_is_not_opencode_auth_or_invalid_warning():
    result = build_opencode_auth_from_llm({"provider": "github_copilot", "api_key": "  ghu_TEST  "})
    assert result.provider == "github-copilot"
    assert result.auth_type == "copilot_plugin_proxy"
    assert result.auth_info is None
    assert result.warning is None


def test_copilot_oauth_is_not_written_to_opencode_auth():
    result = build_opencode_auth_from_llm(
        {"provider": "github_copilot", "oauth": {"type": "oauth", "access": "DIRECT_SECRET", "refresh": "DIRECT_SECRET", "expires": 0}}
    )
    assert result.provider == "github-copilot"
    assert result.auth_type == "copilot_plugin_proxy"
    assert result.auth_info is None


def test_api_provider_strips_api_key():
    result = build_opencode_auth_from_llm({"provider": "openai", "api_key": "  sk_TEST  "})
    assert result.auth_info == {"type": "api", "key": "sk_TEST"}
