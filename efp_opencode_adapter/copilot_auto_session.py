"""Copilot auto model selection for the OpenCode runtime.

``auto`` is not a model the Copilot chat API accepts; it is VS Code's routing
mode. OpenCode sends ``model: "auto"`` (or a tier id, declared in opencode.json
by ``opencode_config``) to the adapter's loopback Copilot proxy, and the proxy
does what current VS Code does behind its Auto picker: ``POST {api}/auto`` with
the turn's prompt and the tier returns the model Copilot picked
(``selected_model``, with that model's metadata), a ``session_token`` and the
discount; the request then goes out with the picked model id and the
``Copilot-Session-Token`` header, which puts it in Copilot's auto billing and
rate-limit pool (10% discount).

A turn is routed once: the proxy keys the decision on the conversation (the
first user message), the tier, the turn (its user message count) and the
member's words, so OpenCode's tool-call continuations and retries reuse it and
the next user message routes afresh. The pick is admitted when this proxy can
drive it (served on ``/responses``, tool calls not refused, its prompt cap
covering the request so far); otherwise, or when the route fails, the request
degrades to ``DEFAULT_AUTO_FALLBACK_MODEL`` without the session header and the
reason is logged once per reason.

Mirrors the native runtime (``efp_runtime/llm/provider.py``
``GitHubCopilotProvider.resolve_auto_model``): keep the admission rules, the
refresh margin and the fallback rules aligned.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Mapping

import aiohttp

from .copilot_auto_catalog import (
    AUTO_MODEL_ID,
    AUTO_MODEL_IDS,
    COPILOT_AUTO_MODEL_CONFIG,
    COPILOT_AUTO_MODEL_CONFIGS,
    COPILOT_AUTO_TIERS,
    DEFAULT_AUTO_FALLBACK_MODEL,
    SUPPORTED_COPILOT_MODEL_IDS,
    allowed_reasoning_efforts,
    auto_pick_refusal,
    auto_tier_for,
    clamp_reasoning_effort,
    copilot_model_limits,
    is_auto_model,
    preferred_model_limits,
    static_model_limits,
)
from .copilot_plugin_auth import COPILOT_PLUGIN_HEADERS, CopilotInternalToken, redact_copilot_secrets
from .outbound_proxy import OutboundProxyConfig

logger = logging.getLogger(__name__)

AUTO_ROUTE_PATH = "/auto"
MODELS_PATH = "/models"
# VS Code's CAPI client stamps this API version on every request; without it
# the auto routes answer 404.
AUTO_API_VERSION = "2026-08-01"
# Route again this close to the session token's expires_at (VS Code's
# five-minute rule; the token lasts 24 h).
AUTO_SESSION_REFRESH_MARGIN_SECONDS = 300
# Routing is one small classification call (VS Code allows five seconds).
AUTO_ROUTE_TIMEOUT_SECONDS = 20
MODELS_CATALOG_TTL_SECONDS = 3600
MODELS_CATALOG_RETRY_SECONDS = 300
MAX_CACHED_DECISIONS = 64
CHARS_PER_TOKEN = 4


class CopilotAutoSessionError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class CopilotAutoDecision:
    """One ``POST /auto`` answer: the model Copilot picked for a prompt and tier."""

    model: str
    session_token: str
    expires_at: int
    tier: str | None
    # The ``selected_model`` entry as returned (a ``GET /models`` row minus
    # ``type``: id, name, vendor, capabilities, supported_endpoints, ...).
    metadata: Mapping[str, Any]
    discounted_costs: Mapping[str, Any]

    def expires_within(self, seconds: int, *, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return self.expires_at - current < seconds


@dataclass(frozen=True)
class AutoRouting:
    """What one auto request is sent as."""

    model: str
    session_token: str | None
    fallback_reason: str | None = None
    tier: str | None = None
    discount: Any = None
    # The efforts the picked model lists; the body's effort is clamped to them.
    reasoning_efforts: tuple[str, ...] | None = None
    # ``{"context", "prompt", "output"}`` tokens the request was admitted under.
    limits: Mapping[str, int] | None = None
    expires_at: int | None = None

    @property
    def session_active(self) -> bool:
        return self.session_token is not None

    def expires_within(self, seconds: int, *, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        current = time.time() if now is None else now
        return self.expires_at - current < seconds


@dataclass(frozen=True)
class AutoTurn:
    """What the proxy reads off a chat body to route it."""

    prompt: str
    conversation_key: str
    turn_index: int
    history_chars: int
    has_image: bool = False


def parse_auto_decision(payload: Any, *, tier: str | None = None) -> CopilotAutoDecision:
    if not isinstance(payload, Mapping):
        raise CopilotAutoSessionError("auto route returned a non-object JSON response")
    session_token = payload.get("session_token")
    if not isinstance(session_token, str) or not session_token.strip():
        raise CopilotAutoSessionError("auto route returned no session_token")
    expires_at = payload.get("expires_at")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)) or expires_at <= 0:
        raise CopilotAutoSessionError("auto route returned an invalid expires_at")
    selected = payload.get("selected_model")
    if not isinstance(selected, Mapping):
        raise CopilotAutoSessionError("auto route returned no selected_model")
    model = selected.get("id")
    if not isinstance(model, str) or not model.strip():
        raise CopilotAutoSessionError("auto route returned a selected_model without an id")
    discounts = payload.get("discounted_costs")
    return CopilotAutoDecision(
        model=model.strip().lower(),
        session_token=session_token.strip(),
        expires_at=int(expires_at),
        tier=tier,
        metadata=dict(selected),
        discounted_costs=dict(discounts) if isinstance(discounts, Mapping) else {},
    )


def parse_models_catalog(payload: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
        raise CopilotAutoSessionError("models request returned no data list")
    catalog: dict[str, dict[str, Any]] = {}
    for row in payload["data"]:
        if not isinstance(row, Mapping):
            continue
        model_id = row.get("id")
        if isinstance(model_id, str) and model_id.strip():
            catalog[model_id.strip().lower()] = dict(row)
    return catalog


def _message_text_and_image(content: Any) -> tuple[str, bool]:
    """Text and whether an image rides along, for a Responses or chat message content."""

    if isinstance(content, str):
        return content.strip(), False
    texts: list[str] = []
    has_image = False
    if isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                texts.append(part)
                continue
            if not isinstance(part, Mapping):
                continue
            kind = str(part.get("type") or "").lower()
            if kind in {"input_text", "text", "output_text"} and isinstance(part.get("text"), str):
                texts.append(part["text"])
            elif kind in {"input_image", "image_url", "image"}:
                has_image = True
    return " ".join(text.strip() for text in texts if text.strip()).strip(), has_image


def _user_messages(payload: Mapping[str, Any]) -> list[tuple[str, bool]]:
    """The user messages of a Responses (``input``) or chat (``messages``) body, in order."""

    items: Any = payload.get("input")
    if isinstance(items, str):
        text = items.strip()
        return [(text, False)] if text else []
    if not isinstance(items, list):
        items = payload.get("messages")
    if not isinstance(items, list):
        return []
    messages: list[tuple[str, bool]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("type") or "message").lower()
        if kind != "message" or str(item.get("role") or "").lower() != "user":
            continue
        text, has_image = _message_text_and_image(item.get("content"))
        if text or has_image:
            messages.append((text, has_image))
    return messages


def auto_turn_from_payload(payload: Mapping[str, Any], *, body_size: int) -> AutoTurn | None:
    """The turn to route for a chat body, or None when it carries no user message.

    The prompt is the last user message (what the member typed this turn);
    the conversation key is the first one (OpenCode resends the whole
    history, so it is stable across the session); the turn index is the user
    message count, so two turns with the same words still route separately.
    """

    messages = _user_messages(payload)
    if not messages:
        return None
    first_text, _ = messages[0]
    prompt, has_image = messages[-1]
    if not prompt:
        prompt = "(image)" if has_image else ""
    if not prompt:
        return None
    key_source = first_text or prompt
    conversation_key = hashlib.sha256(key_source.encode("utf-8")).hexdigest()[:16]
    return AutoTurn(
        prompt=prompt,
        conversation_key=conversation_key,
        turn_index=len(messages),
        history_chars=max(0, int(body_size)),
        has_image=has_image,
    )


class CopilotAutoSessionManager:
    """Routes auto turns through ``POST /auto`` and remembers the decisions."""

    def __init__(self) -> None:
        self._decisions: dict[tuple[Any, ...], AutoRouting] = {}
        self._catalogs: dict[str, tuple[dict[str, dict[str, Any]], float]] = {}
        self._catalog_failed_at: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._last_fallback_reason: str | None = None
        # The most recent routing decision; chat_api reports it as the model
        # that answered an auto request.
        self.last_routing: AutoRouting | None = None

    def invalidate(self, internal_token: CopilotInternalToken | None = None) -> None:
        if internal_token is None:
            self._decisions.clear()
            self._catalogs.clear()
            self._catalog_failed_at.clear()
            return
        token = internal_token.token
        self._decisions = {key: value for key, value in self._decisions.items() if key[0] != token}
        self._catalogs.pop(token, None)
        self._catalog_failed_at.pop(token, None)

    async def route(
        self,
        internal_token: CopilotInternalToken,
        *,
        proxy_config: OutboundProxyConfig,
        turn: AutoTurn,
        tier: str | None,
    ) -> AutoRouting:
        key = (internal_token.token, turn.conversation_key, tier, turn.turn_index, turn.prompt)
        async with self._lock:
            cached = self._decisions.get(key)
            if cached is not None and not cached.expires_within(AUTO_SESSION_REFRESH_MARGIN_SECONDS):
                self.last_routing = cached
                return cached
            try:
                decision = await self._route(
                    internal_token,
                    proxy_config,
                    prompt=turn.prompt,
                    tier=tier,
                    has_image=turn.has_image,
                )
            except CopilotAutoSessionError as exc:
                return self._remember(key, self._fallback(f"auto route unavailable: {exc}", tier=tier))
            model = decision.model
            catalog_entry = (await self._catalog(internal_token, proxy_config)).get(model)
            refusal = auto_pick_refusal(model, decision.metadata, catalog_entry)
            if refusal is not None:
                return self._remember(key, self._fallback(refusal, tier=tier))
            limits = preferred_model_limits(
                copilot_model_limits(catalog_entry),
                copilot_model_limits(decision.metadata),
                static_model_limits(model),
            )
            if limits is None:
                return self._remember(
                    key, self._fallback(f"selected model {model} has no usable context metadata", tier=tier)
                )
            history_tokens = turn.history_chars // CHARS_PER_TOKEN
            if history_tokens > int(limits["prompt"]):
                return self._remember(
                    key,
                    self._fallback(
                        f"selected model {model}: prompt cap {limits['prompt']} tokens is below the request's ~{history_tokens} tokens",
                        tier=tier,
                    ),
                )
            routing = AutoRouting(
                model=model,
                session_token=decision.session_token,
                tier=tier,
                discount=decision.discounted_costs.get(model),
                reasoning_efforts=allowed_reasoning_efforts(decision.metadata, catalog_entry),
                limits=limits,
                expires_at=decision.expires_at,
            )
            self._last_fallback_reason = None
            logger.info(
                "copilot auto (%s): routed to %s (prompt cap %s tokens)",
                tier or "server default",
                model,
                limits["prompt"],
            )
            return self._remember(key, routing)

    def fallback(self, reason: str, *, tier: str | None = None) -> AutoRouting:
        """The default model without a session, for a request that cannot be routed."""

        return self._fallback(reason, tier=tier)

    def _remember(self, key: tuple[Any, ...], routing: AutoRouting) -> AutoRouting:
        self._decisions[key] = routing
        while len(self._decisions) > MAX_CACHED_DECISIONS:
            self._decisions.pop(next(iter(self._decisions)))
        self.last_routing = routing
        return routing

    def _fallback(self, reason: str, *, tier: str | None) -> AutoRouting:
        if reason != self._last_fallback_reason:
            logger.warning("copilot auto: sending %s without a session (%s)", DEFAULT_AUTO_FALLBACK_MODEL, reason)
            self._last_fallback_reason = reason
        routing = AutoRouting(
            model=DEFAULT_AUTO_FALLBACK_MODEL,
            session_token=None,
            fallback_reason=reason,
            tier=tier,
            limits=static_model_limits(DEFAULT_AUTO_FALLBACK_MODEL),
        )
        self.last_routing = routing
        return routing

    async def _route(
        self,
        internal_token: CopilotInternalToken,
        proxy_config: OutboundProxyConfig,
        *,
        prompt: str,
        tier: str | None,
        has_image: bool,
    ) -> CopilotAutoDecision:
        body: dict[str, Any] = {"prompt": prompt}
        if tier:
            body["tier"] = tier
        if has_image:
            body["has_image"] = True
        payload = await self._capi_json(
            internal_token,
            proxy_config,
            "POST",
            AUTO_ROUTE_PATH,
            body=body,
            label="auto route request",
        )
        return parse_auto_decision(payload, tier=tier)

    async def _catalog(
        self,
        internal_token: CopilotInternalToken,
        proxy_config: OutboundProxyConfig,
    ) -> dict[str, dict[str, Any]]:
        """Copilot's model catalog for this token, cached for an hour; empty when unavailable."""

        token = internal_token.token
        now = time.monotonic()
        cached = self._catalogs.get(token)
        if cached is not None and now - cached[1] < MODELS_CATALOG_TTL_SECONDS:
            return cached[0]
        failed_at = self._catalog_failed_at.get(token)
        if failed_at is not None and now - failed_at < MODELS_CATALOG_RETRY_SECONDS:
            return cached[0] if cached is not None else {}
        try:
            payload = await self._capi_json(internal_token, proxy_config, "GET", MODELS_PATH, body=None, label="models request")
            catalog = parse_models_catalog(payload)
        except CopilotAutoSessionError as exc:
            self._catalog_failed_at[token] = now
            logger.warning("copilot models catalog unavailable: %s", exc)
            return cached[0] if cached is not None else {}
        self._catalogs = {token: (catalog, now)}
        self._catalog_failed_at.pop(token, None)
        return catalog

    async def _capi_json(
        self,
        internal_token: CopilotInternalToken,
        proxy_config: OutboundProxyConfig,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None,
        label: str,
    ) -> Any:
        url = f"{internal_token.api_base_url.rstrip('/')}{path}"
        headers = {
            **COPILOT_PLUGIN_HEADERS,
            "Authorization": f"Bearer {internal_token.token}",
            "Accept": "application/json",
            "X-GitHub-Api-Version": AUTO_API_VERSION,
        }
        secrets = [internal_token.token]
        if proxy_config.proxy_url:
            secrets.append(proxy_config.proxy_url)
        try:
            timeout = aiohttp.ClientTimeout(total=AUTO_ROUTE_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout, trust_env=proxy_config.trust_env) as session:
                async with session.request(
                    method,
                    url,
                    headers=headers,
                    json=body,
                    proxy=proxy_config.proxy_url,
                ) as response:
                    status = response.status
                    text = await response.text()
        except Exception as exc:  # noqa: BLE001 - any transport failure is reported as a routing error.
            raise CopilotAutoSessionError(redact_copilot_secrets(f"{label} failed: {exc}", extra=secrets)) from exc
        if status != 200:
            detail = redact_copilot_secrets(text.strip()[:500], extra=secrets)
            raise CopilotAutoSessionError(
                f"{label} failed with HTTP {status}" + (f": {detail}" if detail else ""),
                status_code=status,
            )
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise CopilotAutoSessionError(f"{label} returned invalid JSON") from exc


def _clamp_payload_reasoning(payload: dict[str, Any], allowed: tuple[str, ...] | None) -> None:
    """Clamp the body's reasoning effort (Responses ``reasoning.effort`` or chat ``reasoning_effort``) to what the model lists."""

    if not allowed:
        return
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict) and isinstance(reasoning.get("effort"), str):
        reasoning["effort"] = clamp_reasoning_effort(reasoning["effort"], allowed)
    if isinstance(payload.get("reasoning_effort"), str):
        payload["reasoning_effort"] = clamp_reasoning_effort(payload["reasoning_effort"], allowed)


async def route_auto_request_body(
    manager: CopilotAutoSessionManager,
    body: bytes,
    internal_token: CopilotInternalToken,
    *,
    proxy_config: OutboundProxyConfig,
) -> tuple[bytes, AutoRouting] | None:
    """Rewrite a chat request body whose ``model`` is an auto id; None leaves it untouched.

    Only JSON objects with an auto model are rewritten; anything else (other
    models, non-JSON bodies, non-object JSON) passes through byte for byte.
    The rewritten body carries the picked model and a reasoning effort the
    model accepts.
    """

    if not body:
        return None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not is_auto_model(payload.get("model")):
        return None
    tier = auto_tier_for(payload.get("model"))
    turn = auto_turn_from_payload(payload, body_size=len(body))
    if turn is None:
        routing = manager.fallback("no prompt to route", tier=tier)
    else:
        routing = await manager.route(internal_token, proxy_config=proxy_config, turn=turn, tier=tier)
    payload["model"] = routing.model
    _clamp_payload_reasoning(payload, routing.reasoning_efforts)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), routing


__all__ = [
    "AUTO_API_VERSION",
    "AUTO_MODEL_ID",
    "AUTO_MODEL_IDS",
    "AUTO_ROUTE_PATH",
    "AUTO_ROUTE_TIMEOUT_SECONDS",
    "AUTO_SESSION_REFRESH_MARGIN_SECONDS",
    "COPILOT_AUTO_MODEL_CONFIG",
    "COPILOT_AUTO_MODEL_CONFIGS",
    "COPILOT_AUTO_TIERS",
    "DEFAULT_AUTO_FALLBACK_MODEL",
    "MODELS_PATH",
    "SUPPORTED_COPILOT_MODEL_IDS",
    "AutoRouting",
    "AutoTurn",
    "CopilotAutoDecision",
    "CopilotAutoSessionError",
    "CopilotAutoSessionManager",
    "auto_tier_for",
    "auto_turn_from_payload",
    "is_auto_model",
    "parse_auto_decision",
    "parse_models_catalog",
    "route_auto_request_body",
]
