from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .opencode_config import normalize_opencode_provider_id


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


def _normalize_oauth_payload(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    oauth_type = value.get("type")
    if oauth_type not in (None, "oauth"):
        return None
    refresh = value.get("refresh") if isinstance(value.get("refresh"), str) else ""
    access = value.get("access") if isinstance(value.get("access"), str) else ""
    refresh = refresh.strip()
    access = access.strip()
    if not refresh and access:
        refresh = access
    if not access and refresh:
        access = refresh
    if not refresh or not access:
        return None
    try:
        expires = int(value.get("expires", 0))
        if expires < 0:
            expires = 0
    except Exception:
        expires = 0
    auth_info: dict[str, Any] = {"type": "oauth", "refresh": refresh, "access": access, "expires": expires}
    for extra in ("enterpriseUrl", "accountId"):
        extra_value = value.get(extra)
        if isinstance(extra_value, str) and extra_value.strip():
            auth_info[extra] = extra_value.strip()
    return auth_info


def _selected_copilot_oauth(llm: Mapping[str, Any]) -> dict[str, Any] | None:
    oauth = _normalize_oauth_payload(llm.get("oauth"))
    if oauth:
        return oauth
    oauth_by_runtime = llm.get("oauth_by_runtime")
    if not isinstance(oauth_by_runtime, Mapping):
        return None
    return _normalize_oauth_payload(oauth_by_runtime.get("opencode"))


def build_opencode_auth_from_llm(llm: Mapping[str, Any]) -> AuthBuildResult:
    provider = _provider_from_llm(llm)
    if not provider:
        return AuthBuildResult()
    raw_api_key = llm.get("api_key")
    api_key = raw_api_key.strip() if isinstance(raw_api_key, str) else ""
    if provider == "github-copilot":
        oauth = _selected_copilot_oauth(llm)
        if oauth:
            return AuthBuildResult(provider=provider, auth_info=oauth, auth_type="oauth")
        if api_key.startswith("gho_"):
            return AuthBuildResult(provider=provider, auth_info={"type": "oauth", "refresh": api_key, "access": api_key, "expires": 0}, auth_type="oauth")
        if api_key.startswith("ghu_"):
            return AuthBuildResult(provider=provider, warning="github-copilot received a legacy ghu_ token from Portal copilot/token_verification flow; OpenCode requires oauth auth generated from GitHub device flow")
        if api_key:
            return AuthBuildResult(provider=provider, warning="github-copilot auth skipped because no valid oauth token was provided")
        return AuthBuildResult(provider=provider)

    if api_key:
        return AuthBuildResult(provider=provider, auth_info={"type": "api", "key": api_key}, auth_type="api")
    return AuthBuildResult(provider=provider)


def build_opencode_auth_from_runtime_config(runtime_config: Mapping[str, Any]) -> AuthBuildResult:
    llm = runtime_config.get("llm") if isinstance(runtime_config.get("llm"), dict) else None
    if not isinstance(llm, Mapping):
        return AuthBuildResult()
    return build_opencode_auth_from_llm(llm)
