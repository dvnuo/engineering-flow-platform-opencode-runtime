"""AI Platform opencode proxy: credential file, token manager, config/auth."""
import asyncio
import dataclasses

import pytest

from efp_opencode_adapter import ai_platform_proxy as apx
from efp_opencode_adapter.ai_platform_proxy import (
    AIPlatformCredentialMissing,
    AIPlatformInternalToken,
    AIPlatformTokenManager,
    credential_from_runtime_config,
    load_ai_platform_credential,
    save_or_clear_ai_platform_credential,
)
from efp_opencode_adapter.opencode_auth import build_opencode_auth_from_llm
from efp_opencode_adapter.opencode_config import provider_config_from_runtime_profile
from efp_opencode_adapter.settings import Settings


def _settings(tmp_path):
    return dataclasses.replace(Settings.from_env(), adapter_state_dir=tmp_path)


def _cfg(**auth):
    a = {"username": "u", "password": "pw", "usercase": "uc"}
    a.update(auth)
    return {
        "llm": {
            "provider": "ai-platform",
            "model": "gpt-5.4",
            "ai_platform": {
                "chat": {"host": "https://chat.int", "uri": "/v1/api/v1/chat/completions"},
                "ib2b": {"host": "https://ib2b.int", "uri": "/dsp/token"},
                "auth": a,
            },
        }
    }


def test_credential_roundtrip_and_clear(tmp_path):
    settings = _settings(tmp_path)
    assert save_or_clear_ai_platform_credential(settings, _cfg()) is True
    cred = load_ai_platform_credential(settings)
    assert cred is not None
    assert cred.chat_url == "https://chat.int/v1/api/v1/chat/completions"
    assert cred.ib2b_url == "https://ib2b.int/dsp/token"
    assert cred.password == "pw"
    # a non-ai_platform config clears the file
    assert save_or_clear_ai_platform_credential(settings, {"llm": {"provider": "github_copilot"}}) is False
    assert load_ai_platform_credential(settings) is None


def test_credential_none_without_chat_host():
    assert credential_from_runtime_config({"llm": {"provider": "ai-platform", "ai_platform": {"auth": {"username": "u"}}}}) is None
    assert credential_from_runtime_config({"llm": {"provider": "github_copilot"}}) is None


def test_token_manager_direct_token_needs_no_http(tmp_path):
    # A direct token flows straight through the exchange step (no iB2B call).
    settings = _settings(tmp_path)
    save_or_clear_ai_platform_credential(settings, _cfg(token="JWT-DIRECT"))
    tm = AIPlatformTokenManager(settings)
    tok = asyncio.run(tm.get_token())
    assert tok.token == "JWT-DIRECT"
    assert tok.chat_url == "https://chat.int/v1/api/v1/chat/completions"
    assert tok.trust_token_header  # defaulted


def test_token_manager_exchanges_and_caches(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    save_or_clear_ai_platform_credential(settings, _cfg())
    calls = {"n": 0}

    async def _fake_exchange(credential, **kw):
        calls["n"] += 1
        import time

        return AIPlatformInternalToken(
            token=f"JWT-{calls['n']}",
            expires_at=int(time.time()) + 30,
            chat_url=credential.chat_url,
            trust_token_header=credential.trust_token_header,
            tracking_prefix=credential.tracking_prefix,
            usercase=credential.usercase,
        )

    monkeypatch.setattr(apx, "exchange_ai_platform_token", _fake_exchange)
    tm = AIPlatformTokenManager(settings)
    t1 = asyncio.run(tm.get_token())
    t2 = asyncio.run(tm.get_token())
    assert t1.token == "JWT-1"
    assert t2.token == "JWT-1"  # cached, not re-exchanged
    assert calls["n"] == 1


def test_token_manager_missing_credential_raises(tmp_path):
    tm = AIPlatformTokenManager(_settings(tmp_path))  # no file written
    with pytest.raises(AIPlatformCredentialMissing):
        asyncio.run(tm.get_token())


def test_provider_block_uses_proxy_and_auth_is_proxy(tmp_path):
    pb = provider_config_from_runtime_profile(_cfg(), _settings(tmp_path))
    block = pb["provider"]["ai-platform"]
    assert block["npm"] == "@ai-sdk/openai-compatible"
    assert block["options"]["baseURL"].endswith("/api/internal/ai-platform")
    assert block["options"]["apiKey"] == "efp-ai-platform-proxy"

    auth = build_opencode_auth_from_llm(_cfg()["llm"])
    assert auth.provider == "ai-platform"
    assert auth.auth_type == "ai_platform_proxy"
    assert auth.auth_info is None  # no static key written to auth.json
