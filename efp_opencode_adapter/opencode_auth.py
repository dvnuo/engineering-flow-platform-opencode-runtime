from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .opencode_config import normalize_opencode_provider_id
from .path_utils import path_exists
from .settings import Settings


@dataclass(frozen=True)
class AuthBuildResult:
    provider: str | None = None
    auth_info: dict[str, Any] | None = None
    warning: str | None = None
    auth_type: str | None = None


def _provider_from_llm(llm: Mapping[str, Any]) -> str | None:
    provider = normalize_opencode_provider_id(llm.get("provider"))
    if provider:
        return provider
    model = llm.get("model")
    if isinstance(model, str) and "/" in model:
        prefix = model.split("/", 1)[0]
        provider = normalize_opencode_provider_id(prefix)
    return provider or None


def build_opencode_auth_from_llm(llm: Mapping[str, Any]) -> AuthBuildResult:
    provider = _provider_from_llm(llm)
    if not provider:
        return AuthBuildResult()
    raw_api_key = llm.get("api_key")
    api_key = raw_api_key.strip() if isinstance(raw_api_key, str) else ""
    if provider == "github-copilot":
        return AuthBuildResult(provider=provider, auth_info=None, auth_type="copilot_plugin_proxy")

    if api_key:
        return AuthBuildResult(provider=provider, auth_info={"type": "api", "key": api_key}, auth_type="api")
    return AuthBuildResult(provider=provider)


def build_opencode_auth_from_runtime_config(runtime_config: Mapping[str, Any]) -> AuthBuildResult:
    llm = runtime_config.get("llm") if isinstance(runtime_config.get("llm"), dict) else None
    if not isinstance(llm, Mapping):
        return AuthBuildResult()
    return build_opencode_auth_from_llm(llm)


def clear_opencode_auth_provider(settings: Settings, provider: str) -> bool:
    normalized_provider = normalize_opencode_provider_id(provider)
    if not normalized_provider:
        return False
    auth_path = settings.opencode_data_dir / "auth.json"
    if not path_exists(auth_path):
        return False
    try:
        existing = json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(existing, dict) or normalized_provider not in existing:
        return False
    updated = dict(existing)
    updated.pop(normalized_provider, None)
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = Path(f"{auth_path}.tmp")
    tmp_path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
    tmp_path.chmod(0o600)
    tmp_path.replace(auth_path)
    auth_path.chmod(0o600)
    return True
