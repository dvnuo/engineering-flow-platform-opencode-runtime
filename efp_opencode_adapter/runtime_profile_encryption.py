"""Field-level decryption for the runtime-profile Secret config.

The portal encrypts the sensitive VALUES of the canonical profile config
(api keys, tokens, passwords) as ``ENC:<fernet-token>`` before writing them into
the ``efp-profile-*`` Secret, so operators with broad Secret read access (e.g. a
shared k8s dashboard) see ciphertext instead of live credentials. This runtime
decrypts these values at boot, before projecting/using the config.

This runtime only ever DECRYPTS, so it carries just the decrypt path. Both sides
derive a Fernet key from the raw ``EFP_CONFIG_KEY`` env var
(sha256 -> urlsafe base64); the shared key-derivation core (``ENC_PREFIX``,
``config_encryption_key``, ``_fernet``, ``_decrypt_value``) MUST stay
byte-identical to the portal's ``app/services/profile_secret_encryption.py`` so
this runtime can read the portal's ciphertext. Decryption is driven by the
``ENC:`` prefix (not the field name) and is a no-op when no ``ENC:`` values are
present (plaintext, for dev). The key's protection is an ops concern: keep
EFP_CONFIG_KEY out of the broadly-readable Secret set, otherwise this is
obfuscation rather than isolation.

Self-contained (stdlib + cryptography only).
"""
from __future__ import annotations

import base64
import copy
import hashlib
import os
from typing import Any

ENC_PREFIX = "ENC:"


def config_encryption_key() -> str | None:
    key = os.environ.get("EFP_CONFIG_KEY")
    return key if key else None


def _fernet(key: str):
    from cryptography.fernet import Fernet

    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))


def decrypt_sensitive_fields(config: Any) -> Any:
    """Return a copy of ``config`` with every ``ENC:`` value decrypted.

    Decryption is driven by the ``ENC:`` prefix (not the field name), so it
    recovers any encrypted value. Raises if an ``ENC:`` value is present but
    EFP_CONFIG_KEY is not set.
    """
    result = copy.deepcopy(config)
    if not _has_encrypted_value(result):
        return result
    key = config_encryption_key()
    if not key:
        raise RuntimeError(
            "Found an ENC: value in the profile config but EFP_CONFIG_KEY is not set. "
            "Set EFP_CONFIG_KEY to the correct key before starting."
        )
    _walk_decrypt(result, _fernet(key))
    return result


def _walk_decrypt(obj: Any, fernet) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and v.startswith(ENC_PREFIX):
                obj[k] = _decrypt_value(v, fernet)
            elif isinstance(v, (dict, list)):
                _walk_decrypt(v, fernet)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, str) and item.startswith(ENC_PREFIX):
                obj[i] = _decrypt_value(item, fernet)
            elif isinstance(item, (dict, list)):
                _walk_decrypt(item, fernet)


def _decrypt_value(value: str, fernet) -> str:
    from cryptography.fernet import InvalidToken

    try:
        return fernet.decrypt(value[len(ENC_PREFIX):].encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise RuntimeError(
            "Failed to decrypt an ENC: profile config value; check EFP_CONFIG_KEY."
        ) from exc


def _has_encrypted_value(obj: Any) -> bool:
    if isinstance(obj, dict):
        return any(_has_encrypted_value(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_encrypted_value(v) for v in obj)
    return isinstance(obj, str) and obj.startswith(ENC_PREFIX)
