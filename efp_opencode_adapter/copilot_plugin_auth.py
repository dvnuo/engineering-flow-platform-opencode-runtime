from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import unquote, urlparse

import aiohttp

from .opencode_config import normalize_opencode_provider_id
from .path_utils import path_exists
from .settings import Settings


COPILOT_PLUGIN_HEADERS = {
    "User-Agent": "GitHubCopilotChat/0.35.0",
    "Editor-Version": "vscode/1.107.0",
    "Editor-Plugin-Version": "copilot-chat/0.35.0",
    "Copilot-Integration-Id": "vscode-chat",
}
TOKEN_REFRESH_MARGIN_SECONDS = 300
DEFAULT_COPILOT_API_BASE_URL = "https://api.individual.githubcopilot.com"


@dataclass(frozen=True)
class CopilotSourceCredential:
    credential: str
    source: str
    enterprise_url: str | None = None


@dataclass(frozen=True)
class CopilotCredentialApplyResult:
    provider: str | None
    credential_present: bool
    stored: bool = False
    cleared: bool = False
    enterprise_url_present: bool = False


@dataclass(frozen=True)
class CopilotInternalToken:
    token: str
    expires_at: int
    api_base_url: str


class CopilotCredentialMissing(RuntimeError):
    pass


class CopilotTokenExchangeError(RuntimeError):
    pass


def copilot_plugin_auth_path(settings: Settings) -> Path:
    return settings.adapter_state_dir / "copilot-plugin-auth.json"


def _provider_from_llm(llm: Mapping[str, Any]) -> str | None:
    provider = normalize_opencode_provider_id(llm.get("provider"))
    if provider:
        return provider
    model = llm.get("model")
    if isinstance(model, str) and "/" in model:
        return normalize_opencode_provider_id(model.split("/", 1)[0]) or None
    return None


def provider_from_runtime_config(runtime_config: Mapping[str, Any]) -> str | None:
    llm = runtime_config.get("llm") if isinstance(runtime_config.get("llm"), Mapping) else None
    if not isinstance(llm, Mapping):
        return None
    return _provider_from_llm(llm)


def is_copilot_runtime_config(runtime_config: Mapping[str, Any]) -> bool:
    return provider_from_runtime_config(runtime_config) == "github-copilot"


def _clean_string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def extract_copilot_source_credential(runtime_config: Mapping[str, Any]) -> CopilotSourceCredential | None:
    llm = runtime_config.get("llm") if isinstance(runtime_config.get("llm"), Mapping) else None
    if not isinstance(llm, Mapping) or _provider_from_llm(llm) != "github-copilot":
        return None

    oauth = llm.get("oauth") if isinstance(llm.get("oauth"), Mapping) else None
    enterprise_url = None
    if isinstance(oauth, Mapping):
        raw_enterprise_url = _clean_string(oauth.get("enterpriseUrl"))
        enterprise_url = raw_enterprise_url or None
        refresh = _clean_string(oauth.get("refresh"))
        if refresh:
            return CopilotSourceCredential(credential=refresh, source="oauth_refresh", enterprise_url=enterprise_url)
        access = _clean_string(oauth.get("access"))
        if access:
            return CopilotSourceCredential(credential=access, source="oauth_access", enterprise_url=enterprise_url)

    api_key = _clean_string(llm.get("api_key"))
    if api_key:
        return CopilotSourceCredential(credential=api_key, source="api_key", enterprise_url=enterprise_url)

    return None


def _credential_fingerprint(credential: str) -> str:
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


def save_copilot_plugin_credential(settings: Settings, credential: CopilotSourceCredential) -> Path:
    path = copilot_plugin_auth_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "type": "github_copilot_plugin_oauth",
        "source": credential.source,
        "credential": credential.credential,
        "enterpriseUrl": credential.enterprise_url,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    tmp_path = Path(f"{path}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.chmod(0o600)
    tmp_path.replace(path)
    path.chmod(0o600)
    return path


def clear_copilot_plugin_credential(settings: Settings) -> bool:
    path = copilot_plugin_auth_path(settings)
    if not path_exists(path):
        return False
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def save_or_clear_copilot_plugin_credential(settings: Settings, runtime_config: Mapping[str, Any]) -> CopilotCredentialApplyResult:
    provider = provider_from_runtime_config(runtime_config)
    credential = extract_copilot_source_credential(runtime_config)
    if provider != "github-copilot" or credential is None:
        return CopilotCredentialApplyResult(
            provider=provider,
            credential_present=False,
            cleared=clear_copilot_plugin_credential(settings),
        )
    save_copilot_plugin_credential(settings, credential)
    return CopilotCredentialApplyResult(
        provider=provider,
        credential_present=True,
        stored=True,
        enterprise_url_present=bool(credential.enterprise_url),
    )


def load_copilot_plugin_credential(settings: Settings) -> CopilotSourceCredential | None:
    path = copilot_plugin_auth_path(settings)
    if not path_exists(path):
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, Mapping):
        return None
    credential = _clean_string(payload.get("credential"))
    if not credential:
        return None
    source = _clean_string(payload.get("source")) or "unknown"
    enterprise_url = _clean_string(payload.get("enterpriseUrl")) or None
    return CopilotSourceCredential(credential=credential, source=source, enterprise_url=enterprise_url)


def build_copilot_token_exchange_headers(source_credential: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {source_credential}",
        **COPILOT_PLUGIN_HEADERS,
    }


def copilot_token_exchange_url(settings: Settings) -> str:
    return f"{settings.copilot_github_api_base_url.rstrip('/')}/copilot_internal/v2/token"


def parse_copilot_api_base_url_from_token(token: str) -> str:
    match = re.search(r"(?:^|[;&,\s])proxy-ep=([^;&,\s]+)", token or "")
    if not match:
        return DEFAULT_COPILOT_API_BASE_URL
    raw_value = unquote(match.group(1).strip())
    parsed = urlparse(raw_value)
    host = parsed.netloc or parsed.path.split("/", 1)[0]
    host = host.strip()
    if not host:
        return DEFAULT_COPILOT_API_BASE_URL
    if host.startswith("proxy."):
        host = "api." + host[len("proxy.") :]
    return f"https://{host.rstrip('/')}"


async def exchange_copilot_internal_token(settings: Settings, credential: CopilotSourceCredential) -> CopilotInternalToken:
    url = copilot_token_exchange_url(settings)
    headers = build_copilot_token_exchange_headers(credential.credential)
    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as response:
                status = response.status
                if not 200 <= status < 300:
                    raise CopilotTokenExchangeError(f"copilot token exchange failed with status {status}")
                try:
                    payload = await response.json()
                except Exception as exc:
                    raise CopilotTokenExchangeError("copilot token exchange returned invalid json") from exc
    except CopilotTokenExchangeError:
        raise
    except Exception as exc:
        raise CopilotTokenExchangeError(redact_copilot_secrets(str(exc), extra=[credential.credential])) from exc

    if not isinstance(payload, Mapping):
        raise CopilotTokenExchangeError("copilot token exchange returned invalid response")
    token = _clean_string(payload.get("token"))
    try:
        expires_at = int(payload.get("expires_at"))
    except Exception as exc:
        raise CopilotTokenExchangeError("copilot token exchange returned invalid expiry") from exc
    if not token or expires_at <= 0:
        raise CopilotTokenExchangeError("copilot token exchange returned invalid token")
    return CopilotInternalToken(
        token=token,
        expires_at=expires_at,
        api_base_url=parse_copilot_api_base_url_from_token(token),
    )


class CopilotTokenManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = asyncio.Lock()
        self._cached: CopilotInternalToken | None = None
        self._cached_source_fingerprint: str | None = None

    def _cache_valid(self, credential: CopilotSourceCredential) -> bool:
        if self._cached is None:
            return False
        if self._cached_source_fingerprint != _credential_fingerprint(credential.credential):
            return False
        return self._cached.expires_at - int(time.time()) >= TOKEN_REFRESH_MARGIN_SECONDS

    async def get_token(self) -> CopilotInternalToken:
        credential = load_copilot_plugin_credential(self.settings)
        if credential is None:
            raise CopilotCredentialMissing("copilot credential is not configured")
        if self._cache_valid(credential):
            return self._cached  # type: ignore[return-value]
        async with self._lock:
            credential = load_copilot_plugin_credential(self.settings)
            if credential is None:
                self._cached = None
                self._cached_source_fingerprint = None
                raise CopilotCredentialMissing("copilot credential is not configured")
            if self._cache_valid(credential):
                return self._cached  # type: ignore[return-value]
            token = await exchange_copilot_internal_token(self.settings, credential)
            self._cached = token
            self._cached_source_fingerprint = _credential_fingerprint(credential.credential)
            return token

    def status_snapshot(self) -> dict[str, bool]:
        credential_present = load_copilot_plugin_credential(self.settings) is not None
        cached = self._cached
        return {
            "enabled": credential_present,
            "credential_present": credential_present,
            "token_cached": bool(cached and cached.expires_at > int(time.time())),
            "expires_at_present": bool(cached and cached.expires_at > 0),
        }


def redact_copilot_secrets(text: str, *, extra: Iterable[str] = ()) -> str:
    redacted = str(text)
    for secret in extra:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    redacted = re.sub(r"gh[uo]_[A-Za-z0-9._~+/=-]+", "[redacted]", redacted)
    redacted = re.sub(r"tid=[^;&,\s\"']+", "[redacted]", redacted)
    redacted = re.sub(r"(?i)authorization\s*[:=]\s*bearer\s+[^;&,\s\"']+", "[redacted-header]", redacted)
    redacted = re.sub(r"(?i)\bauthorization\b", "[redacted-header]", redacted)
    redacted = re.sub(r"(?i)\bbearer\s+[^;&,\s\"']+", "Bearer [redacted]", redacted)
    return redacted
