from efp_opencode_adapter.opencode_auth import build_opencode_auth_from_llm


def test_copilot_stripped_gho_token_becomes_oauth():
    result = build_opencode_auth_from_llm({"provider": "github_copilot", "api_key": "  gho_TEST  "})
    assert result.auth_info == {"type": "oauth", "refresh": "gho_TEST", "access": "gho_TEST", "expires": 0}


def test_copilot_stripped_ghu_token_skips_auth_info():
    result = build_opencode_auth_from_llm({"provider": "github_copilot", "api_key": "  ghu_TEST  "})
    assert result.auth_info is None
    assert result.warning
    assert "ghu_TEST" not in result.warning


def test_api_provider_strips_api_key():
    result = build_opencode_auth_from_llm({"provider": "openai", "api_key": "  sk_TEST  "})
    assert result.auth_info == {"type": "api", "key": "sk_TEST"}
