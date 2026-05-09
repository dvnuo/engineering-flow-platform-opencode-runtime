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


def test_copilot_oauth_by_runtime_opencode_becomes_oauth():
    result = build_opencode_auth_from_llm(
        {
            "provider": "github_copilot",
            "oauth_by_runtime": {
                "opencode": {"type": "oauth", "access": "OPENCODE_SECRET", "refresh": "OPENCODE_SECRET", "expires": 0}
            },
        }
    )
    assert result.provider == "github-copilot"
    assert result.auth_type == "oauth"
    assert result.auth_info["access"] == "OPENCODE_SECRET"


def test_copilot_oauth_direct_takes_precedence_over_oauth_by_runtime():
    result = build_opencode_auth_from_llm(
        {
            "provider": "github_copilot",
            "oauth": {"type": "oauth", "access": "DIRECT_SECRET", "refresh": "DIRECT_SECRET", "expires": 0},
            "oauth_by_runtime": {
                "opencode": {"type": "oauth", "access": "OPENCODE_SECRET", "refresh": "OPENCODE_SECRET", "expires": 0}
            },
        }
    )
    assert result.auth_type == "oauth"
    assert result.auth_info["access"] == "DIRECT_SECRET"


def test_copilot_oauth_by_runtime_native_is_not_used_by_opencode_runtime():
    result = build_opencode_auth_from_llm(
        {
            "provider": "github_copilot",
            "oauth_by_runtime": {
                "native": {"type": "oauth", "access": "NATIVE_SECRET", "refresh": "NATIVE_SECRET", "expires": 0}
            },
        }
    )
    assert result.provider == "github-copilot"
    assert result.auth_info is None
