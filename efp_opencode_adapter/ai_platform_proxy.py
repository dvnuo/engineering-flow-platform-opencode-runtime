"""Loopback proxy + token manager for the AI Platform LLM provider.

AI Platform auth is two-legged: username/password/usercase are exchanged at an
iB2B endpoint for a short-lived JWT ("trust token"), which is sent in a
configurable trust-token header on each OpenAI-style /chat/completions call.
Because the JWT is short-lived and must be re-exchanged periodically, it cannot
be baked as a static header into opencode.json. Instead opencode is pointed at
this local loopback proxy (as an OpenAI-compatible provider); the proxy fetches
a fresh trust token per request and forwards to the real chat endpoint.

Mirrors the GitHub Copilot plugin proxy (copilot_proxy.py / CopilotTokenManager).
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import aiohttp
from aiohttp import web

from .app_keys import AI_PLATFORM_TOKEN_MANAGER_KEY, SETTINGS_KEY
from .copilot_proxy import (
    _json_error,
    _request_is_loopback,
    _safe_request_headers,
    _safe_response_headers,
)
from .outbound_proxy import outbound_proxy_config_for_url
from .settings import Settings

DEFAULT_AI_PLATFORM_TOKEN_TTL_SECONDS = 30
AI_PLATFORM_TOKEN_REFRESH_MARGIN_SECONDS = 5
DEFAULT_AI_PLATFORM_TRUST_TOKEN_HEADER = "X-XXXX-E2E-Trust-Token"
DEFAULT_AI_PLATFORM_TRACKING_PREFIX = "EFP"
DEFAULT_AI_PLATFORM_CHAT_URI = "/v1/api/v1/chat/completions"


class AIPlatformCredentialMissing(RuntimeError):
    pass


class AIPlatformTokenExchangeError(RuntimeError):
    pass


@dataclass(frozen=True)
class AIPlatformCredential:
    chat_url: str
    ib2b_url: str = ""
    username: str = ""
    password: str = ""
    usercase: str = ""
    token: str = ""
    trust_token_header: str = DEFAULT_AI_PLATFORM_TRUST_TOKEN_HEADER
    tracking_prefix: str = DEFAULT_AI_PLATFORM_TRACKING_PREFIX
    token_ttl_seconds: int = DEFAULT_AI_PLATFORM_TOKEN_TTL_SECONDS

    def fingerprint(self) -> str:
        return "|".join(
            [self.chat_url, self.ib2b_url, self.username, self.password, self.token]
        )


@dataclass(frozen=True)
class AIPlatformInternalToken:
    token: str
    expires_at: int
    chat_url: str
    trust_token_header: str
    tracking_prefix: str
    usercase: str


def ai_platform_auth_path(settings: Settings) -> Path:
    return settings.adapter_state_dir / "ai-platform-auth.json"


def _join_url(host: str, uri: str) -> str:
    host = str(host or "").rstrip("/")
    uri = str(uri or "").strip()
    if uri.startswith("http://") or uri.startswith("https://"):
        return uri
    if uri and not uri.startswith("/"):
        uri = "/" + uri
    return host + uri


def credential_from_runtime_config(runtime_config: Mapping[str, Any]) -> AIPlatformCredential | None:
    """Build the AI Platform credential from a projected runtime config, or None."""
    llm = runtime_config.get("llm") if isinstance(runtime_config, Mapping) else None
    if not isinstance(llm, Mapping):
        return None
    ap = llm.get("ai_platform") if isinstance(llm.get("ai_platform"), Mapping) else None
    if not isinstance(ap, Mapping):
        return None
    chat = ap.get("chat") if isinstance(ap.get("chat"), Mapping) else {}
    ib2b = ap.get("ib2b") if isinstance(ap.get("ib2b"), Mapping) else {}
    auth = ap.get("auth") if isinstance(ap.get("auth"), Mapping) else {}
    chat_host = str(chat.get("host") or "").strip()
    if not chat_host:
        return None
    ib2b_host = str(ib2b.get("host") or "").strip()
    return AIPlatformCredential(
        chat_url=_join_url(chat_host, str(chat.get("uri") or DEFAULT_AI_PLATFORM_CHAT_URI)),
        ib2b_url=_join_url(ib2b_host, str(ib2b.get("uri") or "")) if ib2b_host else "",
        username=str(auth.get("username") or "").strip(),
        password=str(auth.get("password") or "").strip(),
        usercase=str(auth.get("usercase") or "").strip(),
        token=str(auth.get("token") or "").strip(),
        trust_token_header=str(auth.get("trust_token_header") or "").strip() or DEFAULT_AI_PLATFORM_TRUST_TOKEN_HEADER,
        tracking_prefix=str(auth.get("tracking_prefix") or "").strip() or DEFAULT_AI_PLATFORM_TRACKING_PREFIX,
    )


def save_or_clear_ai_platform_credential(settings: Settings, runtime_config: Mapping[str, Any]) -> bool:
    """Persist the AI Platform credential file for the proxy, or remove it.

    Returns True when a credential was written.
    """
    path = ai_platform_auth_path(settings)
    credential = credential_from_runtime_config(runtime_config)
    if credential is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "chat_url": credential.chat_url,
        "ib2b_url": credential.ib2b_url,
        "username": credential.username,
        "password": credential.password,
        "usercase": credential.usercase,
        "token": credential.token,
        "trust_token_header": credential.trust_token_header,
        "tracking_prefix": credential.tracking_prefix,
        "token_ttl_seconds": credential.token_ttl_seconds,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return True


def load_ai_platform_credential(settings: Settings) -> AIPlatformCredential | None:
    path = ai_platform_auth_path(settings)
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not str(data.get("chat_url") or "").strip():
        return None
    return AIPlatformCredential(
        chat_url=str(data.get("chat_url") or "").strip(),
        ib2b_url=str(data.get("ib2b_url") or "").strip(),
        username=str(data.get("username") or "").strip(),
        password=str(data.get("password") or "").strip(),
        usercase=str(data.get("usercase") or "").strip(),
        token=str(data.get("token") or "").strip(),
        trust_token_header=str(data.get("trust_token_header") or "").strip() or DEFAULT_AI_PLATFORM_TRUST_TOKEN_HEADER,
        tracking_prefix=str(data.get("tracking_prefix") or "").strip() or DEFAULT_AI_PLATFORM_TRACKING_PREFIX,
        token_ttl_seconds=int(data.get("token_ttl_seconds") or DEFAULT_AI_PLATFORM_TOKEN_TTL_SECONDS),
    )


async def exchange_ai_platform_token(
    credential: AIPlatformCredential, *, trust_env: bool = True, proxy_url: str | None = None
) -> AIPlatformInternalToken:
    now = int(time.time())
    if credential.token:
        return AIPlatformInternalToken(
            token=credential.token,
            expires_at=now + max(1, credential.token_ttl_seconds),
            chat_url=credential.chat_url,
            trust_token_header=credential.trust_token_header,
            tracking_prefix=credential.tracking_prefix,
            usercase=credential.usercase,
        )
    if not (credential.username and credential.password and credential.ib2b_url):
        raise AIPlatformCredentialMissing(
            "AI Platform requires a token, or username/password plus an iB2B endpoint."
        )
    body = {
        "input_token_state": {
            "token_type": "CREDENTIAL",
            "username": credential.username,
            "password": credential.password,
        },
        "output_token_state": {"token_type": "JWT"},
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), trust_env=trust_env) as session:
            async with session.post(
                credential.ib2b_url,
                json=body,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                proxy=proxy_url,
            ) as response:
                if response.status >= 400:
                    raise AIPlatformTokenExchangeError(
                        f"AI Platform iB2B exchange returned {response.status}"
                    )
                data = await response.json(content_type=None)
    except AIPlatformTokenExchangeError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced as an exchange error.
        raise AIPlatformTokenExchangeError(f"AI Platform iB2B exchange failed: {exc}") from exc
    token = str((data or {}).get("issued_token") or "").strip() if isinstance(data, dict) else ""
    if not token:
        raise AIPlatformTokenExchangeError("AI Platform iB2B exchange did not return issued_token.")
    return AIPlatformInternalToken(
        token=token,
        expires_at=now + max(1, credential.token_ttl_seconds),
        chat_url=credential.chat_url,
        trust_token_header=credential.trust_token_header,
        tracking_prefix=credential.tracking_prefix,
        usercase=credential.usercase,
    )


class AIPlatformTokenManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = asyncio.Lock()
        self._cached: AIPlatformInternalToken | None = None
        self._cached_fingerprint: str | None = None

    def _cache_valid(self, credential: AIPlatformCredential) -> bool:
        if self._cached is None:
            return False
        if self._cached_fingerprint != credential.fingerprint():
            return False
        return self._cached.expires_at - int(time.time()) >= AI_PLATFORM_TOKEN_REFRESH_MARGIN_SECONDS

    async def get_token(self) -> AIPlatformInternalToken:
        credential = load_ai_platform_credential(self.settings)
        if credential is None:
            raise AIPlatformCredentialMissing("AI Platform credential is not configured")
        if self._cache_valid(credential):
            return self._cached  # type: ignore[return-value]
        async with self._lock:
            credential = load_ai_platform_credential(self.settings)
            if credential is None:
                self._cached = None
                self._cached_fingerprint = None
                raise AIPlatformCredentialMissing("AI Platform credential is not configured")
            if self._cache_valid(credential):
                return self._cached  # type: ignore[return-value]
            # Route the iB2B exchange through the same outbound proxy the chat
            # forwarding path uses (envs that need an egress proxy also need it
            # for the STS call).
            proxy_config = outbound_proxy_config_for_url(
                self.settings, credential.ib2b_url or credential.chat_url
            )
            token = await exchange_ai_platform_token(
                credential, trust_env=proxy_config.trust_env, proxy_url=proxy_config.proxy_url
            )
            self._cached = token
            self._cached_fingerprint = credential.fingerprint()
            return token

    def status_snapshot(self) -> dict[str, bool]:
        credential_present = load_ai_platform_credential(self.settings) is not None
        cached = self._cached
        return {
            "enabled": credential_present,
            "credential_present": credential_present,
            "token_cached": bool(cached and cached.expires_at > int(time.time())),
        }


def _redact(text: str, secrets: list[str]) -> str:
    out = str(text)
    for secret in secrets:
        if secret:
            out = out.replace(secret, "[redacted]")
    return out


def _tracking_id(prefix: str) -> str:
    return "{0}-{1}".format(prefix or DEFAULT_AI_PLATFORM_TRACKING_PREFIX, time.strftime("%Y%m%d%H%M%S", time.gmtime()))


async def ai_platform_proxy_handler(request: web.Request) -> web.StreamResponse:
    if not _request_is_loopback(request):
        return _json_error(403, "forbidden")
    if request.method == "OPTIONS":
        return web.Response(status=204)
    if request.method not in {"GET", "POST"}:
        return _json_error(405, "method not allowed")

    token_manager = request.app[AI_PLATFORM_TOKEN_MANAGER_KEY]
    try:
        internal = await token_manager.get_token()
    except AIPlatformCredentialMissing:
        return _json_error(401, "ai platform credential not configured")
    except AIPlatformTokenExchangeError as exc:
        return _json_error(502, "ai platform token exchange failed", str(exc))
    except Exception as exc:  # noqa: BLE001
        return _json_error(502, "ai platform proxy unavailable", str(exc))

    # AI Platform exposes a single chat endpoint; forward every call there
    # regardless of the OpenAI-compatible tail the client appended.
    upstream = internal.chat_url
    settings: Settings = request.app[SETTINGS_KEY]
    proxy_config = outbound_proxy_config_for_url(settings, upstream)
    tracking = _tracking_id(internal.tracking_prefix)
    outbound_headers = _safe_request_headers(request.headers)
    outbound_headers.update(
        {
            "Content-Type": "application/json",
            internal.trust_token_header: internal.token,
            "x-correlation-id": tracking,
            "x-usersession-id": tracking,
        }
    )
    body = await request.read()
    # opencode's OpenAI-compatible client does not know the AI Platform
    # "usercase"; inject it as the request `user` field if configured and absent.
    if internal.usercase and body:
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict) and not parsed.get("user"):
                parsed["user"] = internal.usercase
                body = json.dumps(parsed).encode("utf-8")
        except (ValueError, TypeError):
            pass

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None), trust_env=proxy_config.trust_env) as session:
            async with session.request(
                request.method,
                upstream,
                headers=outbound_headers,
                data=body if body else None,
                proxy=proxy_config.proxy_url,
            ) as upstream_response:
                response_headers = _safe_response_headers(upstream_response.headers)
                content_type = upstream_response.headers.get("Content-Type", "")
                if "text/event-stream" in content_type.lower():
                    stream = web.StreamResponse(status=upstream_response.status, headers=response_headers)
                    await stream.prepare(request)
                    async for chunk in upstream_response.content.iter_chunked(8192):
                        await stream.write(chunk)
                    await stream.write_eof()
                    return stream
                response_body = await upstream_response.read()
                if upstream_response.status >= 400:
                    response_body = _redact(
                        response_body.decode("utf-8", errors="replace"), [internal.token]
                    ).encode("utf-8")
                return web.Response(status=upstream_response.status, body=response_body, headers=response_headers)
    except Exception as exc:  # noqa: BLE001
        return _json_error(502, "ai platform upstream request failed", _redact(str(exc), [internal.token]))
