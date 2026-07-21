"""Boot-time decryption of ENC:-prefixed profile Secret values.

The portal writes sensitive canonical-config values (api_key/token/password/...)
into the profile Secret as ``ENC:<fernet-token>``, derived from EFP_CONFIG_KEY.
This runtime must decrypt them at boot, immediately after parsing the payload
config and BEFORE the per-runtime projection, so all downstream building sees
plaintext.
"""
import base64
import hashlib
import json

import pytest

from efp_opencode_adapter.portal_runtime_context_bootstrap import apply_boot_projection
from efp_opencode_adapter.runtime_profile_encryption import (
    decrypt_sensitive_fields,
    encrypt_sensitive_fields,
)
from efp_opencode_adapter.runtime_profile_projection import project_canonical_for_runtime
from efp_opencode_adapter.settings import Settings


TEST_KEY = "unit-test-config-key"


def _enc(value: str, key: str = TEST_KEY) -> str:
    """Build an ENC: token the same way the portal encryption does."""
    from cryptography.fernet import Fernet

    fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))
    return "ENC:" + fernet.encrypt(value.encode()).decode()


def _payload(config: dict, profile_id="rp-enc", revision=1) -> dict:
    return {
        "runtime_profile_id": profile_id,
        "name": "profile",
        "revision": revision,
        "runtime_type": "opencode",
        "config": config,
    }


def test_enc_values_decrypt_before_projection(monkeypatch):
    monkeypatch.setenv("EFP_CONFIG_KEY", TEST_KEY)
    config = {
        "llm": {"provider": "github_copilot", "model": "gpt-x", "api_key": _enc("gho_SECRET")},
        "github": {"enabled": True, "token": _enc("github_pat_SECRET")},
    }

    decrypted = decrypt_sensitive_fields(config)
    assert decrypted["llm"]["api_key"] == "gho_SECRET"
    assert decrypted["github"]["token"] == "github_pat_SECRET"
    # Input is not mutated (deep copy).
    assert config["llm"]["api_key"].startswith("ENC:")

    # Order matters: decryption feeds the projection with plaintext.
    projected = project_canonical_for_runtime(decrypted, "opencode")
    assert projected["llm"]["api_key"] == "gho_SECRET"
    assert projected["llm"]["model"] == "github-copilot/gpt-x"


def test_no_enc_values_is_noop(monkeypatch):
    # Even without EFP_CONFIG_KEY, plaintext config passes through untouched.
    monkeypatch.delenv("EFP_CONFIG_KEY", raising=False)
    config = {"llm": {"provider": "github_copilot", "model": "gpt-x", "api_key": "plain"}}
    result = decrypt_sensitive_fields(config)
    assert result == config
    assert result is not config  # deep copy


def test_enc_value_without_key_raises(monkeypatch):
    monkeypatch.delenv("EFP_CONFIG_KEY", raising=False)
    config = {"llm": {"api_key": _enc("gho_SECRET")}}
    with pytest.raises(RuntimeError, match="EFP_CONFIG_KEY is not set"):
        decrypt_sensitive_fields(config)


def test_enc_value_with_wrong_key_raises(monkeypatch):
    monkeypatch.setenv("EFP_CONFIG_KEY", "wrong-key")
    config = {"llm": {"api_key": _enc("gho_SECRET", key=TEST_KEY)}}
    with pytest.raises(RuntimeError, match="Failed to decrypt"):
        decrypt_sensitive_fields(config)


def test_encrypt_decrypt_round_trip(monkeypatch):
    monkeypatch.setenv("EFP_CONFIG_KEY", TEST_KEY)
    config = {"llm": {"provider": "github_copilot", "api_key": "gho_SECRET"}}
    encrypted = encrypt_sensitive_fields(config)
    assert encrypted["llm"]["api_key"].startswith("ENC:")
    assert decrypt_sensitive_fields(encrypted)["llm"]["api_key"] == "gho_SECRET"


def test_boot_projection_decrypts_enc_secret_end_to_end(monkeypatch):
    # ENC: values in the payload are decrypted at boot so opencode.json / auth
    # build against plaintext credentials.
    monkeypatch.setenv("EFP_CONFIG_KEY", TEST_KEY)
    settings = Settings.from_env()
    config = {
        "llm": {
            "provider": "github_copilot",
            "model": "gpt-x",
            "api_key": _enc("gho_SECRET"),
            "base_url": "http://litellm.local/v1",
        },
        "github": {"enabled": True, "username": "efp-bot", "token": _enc("github_pat_SECRET")},
    }
    result = apply_boot_projection(settings, _payload(config))
    # The env carries the decrypted github token, never the ciphertext.
    assert result.env["GH_TOKEN"] == "github_pat_SECRET"
    cfg_text = settings.opencode_config_path.read_text(encoding="utf-8")
    assert "ENC:" not in cfg_text


def test_boot_projection_fails_loud_when_key_missing(monkeypatch):
    monkeypatch.delenv("EFP_CONFIG_KEY", raising=False)
    settings = Settings.from_env()
    config = {"llm": {"provider": "github_copilot", "model": "gpt-x", "api_key": _enc("gho_SECRET")}}
    with pytest.raises(RuntimeError, match="EFP_CONFIG_KEY is not set"):
        apply_boot_projection(settings, _payload(config))
