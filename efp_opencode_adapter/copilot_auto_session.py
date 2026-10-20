"""Copilot auto model selection for the OpenCode runtime.

``auto`` is not a model the Copilot chat API accepts; it is VS Code's routing
mode. OpenCode sends ``model: "auto"`` (declared in opencode.json by
``opencode_config``) to the adapter's loopback Copilot proxy, and the proxy does
what VS Code does: ``POST {api}/models/session`` returns the session's pool
(``available_models``), a ``session_token`` and ``expires_at``; the request then
goes out with the first preferred GPT-6 model in that pool and the
``Copilot-Session-Token`` header, which puts it in Copilot's auto billing and
rate-limit pool (10% discount).

The pool also lists Claude, Gemini and MAI models this runtime cannot drive, so
only ``SUPPORTED_COPILOT_MODEL_IDS`` are ever chosen. When the session cannot
be opened (the endpoint refusing the token, a network failure) or the pool
holds no supported model, the request degrades to ``DEFAULT_AUTO_FALLBACK_MODEL``
without the session header and the reason is logged once per reason.

Mirrors the native runtime (``efp_runtime/llm/provider.py``
``GitHubCopilotProvider.resolve_auto_model``): keep the preference list, the
refresh margin and the fallback rules aligned.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Mapping

import aiohttp

from .copilot_auto_catalog import (
    AUTO_MODEL_ID,
    COPILOT_AUTO_MODEL_CONFIG,
    COPILOT_AUTO_PREFERENCE,
    DEFAULT_AUTO_FALLBACK_MODEL,
    SUPPORTED_COPILOT_MODEL_IDS,
    is_auto_model,
    select_auto_model,
)
from .copilot_plugin_auth import COPILOT_PLUGIN_HEADERS, CopilotInternalToken, redact_copilot_secrets
from .outbound_proxy import OutboundProxyConfig

logger = logging.getLogger(__name__)

AUTO_SESSION_PATH = "/models/session"
# Reopen the session this close to expires_at (VS Code uses the same margin).
AUTO_SESSION_REFRESH_MARGIN_SECONDS = 300
AUTO_SESSION_TIMEOUT_SECONDS = 30


class CopilotAutoSessionError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class CopilotAutoSession:
    session_token: str
    expires_at: int
    available_models: tuple[str, ...]
    discounted_costs: Mapping[str, Any]

    def expires_within(self, seconds: int, *, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return self.expires_at - current < seconds


@dataclass(frozen=True)
class AutoRouting:
    """What one ``auto`` request is sent as."""

    model: str
    session_token: str | None
    fallback_reason: str | None = None

    @property
    def session_active(self) -> bool:
        return self.session_token is not None


def parse_auto_session(payload: Any) -> CopilotAutoSession:
    if not isinstance(payload, Mapping):
        raise CopilotAutoSessionError("auto session returned a non-object JSON response")
    session_token = payload.get("session_token")
    if not isinstance(session_token, str) or not session_token.strip():
        raise CopilotAutoSessionError("auto session returned no session_token")
    expires_at = payload.get("expires_at")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)) or expires_at <= 0:
        raise CopilotAutoSessionError("auto session returned an invalid expires_at")
    raw_models = payload.get("available_models")
    available = (
        tuple(entry.strip() for entry in raw_models if isinstance(entry, str) and entry.strip())
        if isinstance(raw_models, list)
        else ()
    )
    discounts = payload.get("discounted_costs")
    return CopilotAutoSession(
        session_token=session_token.strip(),
        expires_at=int(expires_at),
        available_models=available,
        discounted_costs=dict(discounts) if isinstance(discounts, Mapping) else {},
    )


class CopilotAutoSessionManager:
    """Opens, caches and refreshes the auto session for the current Copilot token."""

    def __init__(self) -> None:
        self._sessions: dict[str, CopilotAutoSession] = {}
        self._lock = asyncio.Lock()
        self._last_fallback_reason: str | None = None
        # The most recent routing decision; chat_api reports it as the model
        # that answered an auto request (the pick is deterministic per session
        # pool, so concurrent requests on one token share it).
        self.last_routing: AutoRouting | None = None

    async def get_session(
        self,
        internal_token: CopilotInternalToken,
        *,
        proxy_config: OutboundProxyConfig,
        force: bool = False,
    ) -> CopilotAutoSession:
        async with self._lock:
            cached = self._sessions.get(internal_token.token)
            if (
                cached is not None
                and not force
                and not cached.expires_within(AUTO_SESSION_REFRESH_MARGIN_SECONDS)
            ):
                return cached
            session = await self._open(internal_token, proxy_config)
            # A refreshed Copilot token gets its own session; drop the old ones.
            self._sessions = {internal_token.token: session}
            return session

    def invalidate(self, internal_token: CopilotInternalToken | None = None) -> None:
        if internal_token is None:
            self._sessions.clear()
        else:
            self._sessions.pop(internal_token.token, None)

    async def route(
        self,
        internal_token: CopilotInternalToken,
        *,
        proxy_config: OutboundProxyConfig,
    ) -> AutoRouting:
        try:
            session = await self.get_session(internal_token, proxy_config=proxy_config)
        except CopilotAutoSessionError as exc:
            return self._fallback(f"auto session unavailable: {exc}")
        model = select_auto_model(session.available_models)
        if model is None:
            pool = ", ".join(session.available_models) or "empty"
            return self._fallback(f"no supported GPT model in the auto pool ({pool})")
        self._last_fallback_reason = None
        routing = AutoRouting(model=model, session_token=session.session_token)
        self.last_routing = routing
        return routing

    def _fallback(self, reason: str) -> AutoRouting:
        if reason != self._last_fallback_reason:
            logger.warning("copilot auto: sending %s without a session (%s)", DEFAULT_AUTO_FALLBACK_MODEL, reason)
            self._last_fallback_reason = reason
        routing = AutoRouting(model=DEFAULT_AUTO_FALLBACK_MODEL, session_token=None, fallback_reason=reason)
        self.last_routing = routing
        return routing

    async def _open(self, internal_token: CopilotInternalToken, proxy_config: OutboundProxyConfig) -> CopilotAutoSession:
        url = f"{internal_token.api_base_url.rstrip('/')}{AUTO_SESSION_PATH}"
        headers = {
            **COPILOT_PLUGIN_HEADERS,
            "Authorization": f"Bearer {internal_token.token}",
            "Accept": "application/json",
        }
        secrets = [internal_token.token]
        if proxy_config.proxy_url:
            secrets.append(proxy_config.proxy_url)
        try:
            timeout = aiohttp.ClientTimeout(total=AUTO_SESSION_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout, trust_env=proxy_config.trust_env) as session:
                async with session.post(
                    url,
                    headers=headers,
                    json={"auto_mode": {"model_hints": [AUTO_MODEL_ID]}},
                    proxy=proxy_config.proxy_url,
                ) as response:
                    status = response.status
                    text = await response.text()
        except Exception as exc:  # noqa: BLE001 - any transport failure is reported as a session error.
            raise CopilotAutoSessionError(
                redact_copilot_secrets(f"auto session request failed: {exc}", extra=secrets)
            ) from exc
        if status != 200:
            detail = redact_copilot_secrets(text.strip()[:500], extra=secrets)
            raise CopilotAutoSessionError(
                f"auto session request failed with HTTP {status}" + (f": {detail}" if detail else ""),
                status_code=status,
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CopilotAutoSessionError("auto session returned invalid JSON") from exc
        return parse_auto_session(payload)


async def route_auto_request_body(
    manager: CopilotAutoSessionManager,
    body: bytes,
    internal_token: CopilotInternalToken,
    *,
    proxy_config: OutboundProxyConfig,
) -> tuple[bytes, AutoRouting] | None:
    """Rewrite a chat request body whose ``model`` is auto; None leaves it untouched.

    Only JSON objects with an auto model are rewritten; anything else (other
    models, non-JSON bodies, non-object JSON) passes through byte for byte.
    """

    if not body:
        return None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not is_auto_model(payload.get("model")):
        return None
    routing = await manager.route(internal_token, proxy_config=proxy_config)
    payload["model"] = routing.model
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), routing


__all__ = [
    "AUTO_MODEL_ID",
    "AUTO_SESSION_PATH",
    "AUTO_SESSION_REFRESH_MARGIN_SECONDS",
    "COPILOT_AUTO_MODEL_CONFIG",
    "COPILOT_AUTO_PREFERENCE",
    "DEFAULT_AUTO_FALLBACK_MODEL",
    "SUPPORTED_COPILOT_MODEL_IDS",
    "AutoRouting",
    "CopilotAutoSession",
    "CopilotAutoSessionError",
    "CopilotAutoSessionManager",
    "is_auto_model",
    "parse_auto_session",
    "route_auto_request_body",
    "select_auto_model",
]
