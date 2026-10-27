"""The member's Copilot plan quota, read from GitHub's ``copilot_internal/user``.

VS Code and the community Copilot tools read ``GET {github_api}/copilot_internal/user``
with the member's GitHub OAuth token to show how much of the plan is left:
the answer carries ``quota_snapshots`` (one entry per quota id such as
``premium_interactions``, ``chat`` or ``completions``, each with
``entitlement`` / ``remaining`` / ``percent_remaining`` / ``unlimited`` and
overage fields), a ``quota_reset_date`` and the plan name. It is an internal
API without a published contract, so everything here parses defensively:
unknown shapes degrade to ``available: false`` with an error string, never an
exception, and the top-level keys seen ride along as ``raw_keys`` so an
unexpected answer can be understood from the Portal.

The credential is the same one the token exchange uses (the Portal-provided
``api_key`` or OAuth token of the Copilot runtime profile), so the quota is
that member's. Answers are cached for a minute; failures for a few seconds.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timezone
from typing import Any, Mapping

import aiohttp
from aiohttp import web

from .app_keys import COPILOT_QUOTA_CACHE_KEY, SETTINGS_KEY
from .copilot_plugin_auth import (
    CopilotSourceCredential,
    build_copilot_token_exchange_headers,
    load_copilot_plugin_credential,
    redact_copilot_secrets,
)
from .outbound_proxy import outbound_proxy_config_for_url
from .settings import Settings

QUOTA_PATH = "/copilot_internal/user"
QUOTA_SOURCE = "copilot_internal_user"
QUOTA_TIMEOUT_SECONDS = 15
QUOTA_CACHE_TTL_SECONDS = 60
QUOTA_FAILURE_CACHE_TTL_SECONDS = 15

SNAPSHOT_LABELS = {
    "premium_interactions": "Premium requests",
    "chat": "Chat",
    "completions": "Code completions",
    "ai_credits": "AI credits",
    "credits": "AI credits",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _label_for(snapshot_id: str) -> str:
    known = SNAPSHOT_LABELS.get(snapshot_id)
    if known:
        return known
    return snapshot_id.replace("_", " ").strip().capitalize() or "Quota"


def _parse_snapshot(snapshot_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    entitlement = _number(raw.get("entitlement"))
    if entitlement is None:
        entitlement = _number(raw.get("quota_entitlement"))
    remaining = _number(raw.get("remaining"))
    if remaining is None:
        remaining = _number(raw.get("quota_remaining"))
    percent_remaining = _number(raw.get("percent_remaining"))
    unlimited = bool(raw.get("unlimited"))
    used = None
    if not unlimited and entitlement is not None and remaining is not None:
        used = max(0.0, entitlement - remaining)
    if percent_remaining is None and not unlimited and entitlement and remaining is not None:
        percent_remaining = round(max(0.0, min(100.0, remaining / entitlement * 100.0)), 1)
    return {
        "id": snapshot_id,
        "label": _label_for(snapshot_id),
        "entitlement": entitlement,
        "remaining": remaining,
        "used": used,
        "percent_remaining": percent_remaining,
        "unlimited": unlimited,
        "overage_count": _number(raw.get("overage_count")),
        "overage_permitted": raw.get("overage_permitted") if isinstance(raw.get("overage_permitted"), bool) else None,
    }


def _iter_snapshots(raw: Any) -> list[tuple[str, Mapping[str, Any]]]:
    if isinstance(raw, Mapping):
        return [(str(key), value) for key, value in raw.items() if isinstance(value, Mapping)]
    if isinstance(raw, list):
        found: list[tuple[str, Mapping[str, Any]]] = []
        for entry in raw:
            if not isinstance(entry, Mapping):
                continue
            snapshot_id = _text(entry.get("quota_id")) or _text(entry.get("id")) or _text(entry.get("name"))
            if snapshot_id:
                found.append((snapshot_id, entry))
        return found
    return []


def unavailable_quota(error: str, *, fetched_at: str | None = None) -> dict[str, Any]:
    return {
        "available": False,
        "source": QUOTA_SOURCE,
        "fetched_at": fetched_at or _now_iso(),
        "plan": None,
        "reset_date": None,
        "snapshots": [],
        "credits": None,
        "raw_keys": [],
        "error": error,
    }


def parse_copilot_quota(payload: Any, *, fetched_at: str | None = None) -> dict[str, Any]:
    """The quota payload the Portal reads, from whatever ``copilot_internal/user`` answered."""

    stamp = fetched_at or _now_iso()
    if not isinstance(payload, Mapping):
        return unavailable_quota("copilot quota returned a non-object JSON response", fetched_at=stamp)
    snapshots = [_parse_snapshot(snapshot_id, raw) for snapshot_id, raw in _iter_snapshots(payload.get("quota_snapshots"))]
    credits = None
    for snapshot in snapshots:
        if "credit" not in snapshot["id"].lower():
            continue
        credits = {
            "included": snapshot["entitlement"],
            "used": snapshot["used"],
            "remaining": snapshot["remaining"],
            "unlimited": snapshot["unlimited"],
        }
        break
    plan = _text(payload.get("copilot_plan")) or _text(payload.get("plan")) or _text(payload.get("access_type_sku"))
    reset_date = _text(payload.get("quota_reset_date")) or _text(payload.get("quota_reset_date_utc")) or _text(payload.get("reset_date"))
    return {
        "available": True,
        "source": QUOTA_SOURCE,
        "fetched_at": stamp,
        "plan": plan,
        "reset_date": reset_date,
        "snapshots": snapshots,
        "credits": credits,
        "raw_keys": sorted(str(key) for key in payload.keys()),
        "error": None,
    }


def copilot_quota_url(settings: Settings) -> str:
    return f"{settings.copilot_github_api_base_url.rstrip('/')}{QUOTA_PATH}"


async def fetch_copilot_quota(settings: Settings, credential: CopilotSourceCredential) -> dict[str, Any]:
    """One ``GET copilot_internal/user``; never raises, a failure is ``available: false`` with the reason."""

    url = copilot_quota_url(settings)
    headers = build_copilot_token_exchange_headers(credential.credential)
    proxy_config = outbound_proxy_config_for_url(settings, url)
    secrets = [credential.credential]
    if proxy_config.proxy_url:
        secrets.append(proxy_config.proxy_url)
    fetched_at = _now_iso()
    try:
        timeout = aiohttp.ClientTimeout(total=QUOTA_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=proxy_config.trust_env) as session:
            async with session.get(url, headers=headers, proxy=proxy_config.proxy_url) as response:
                status = response.status
                if not 200 <= status < 300:
                    return unavailable_quota(f"copilot quota request failed with HTTP {status}", fetched_at=fetched_at)
                try:
                    payload = await response.json(content_type=None)
                except Exception:  # noqa: BLE001 - a non-JSON body is one more "unavailable".
                    return unavailable_quota("copilot quota returned invalid json", fetched_at=fetched_at)
    except Exception as exc:  # noqa: BLE001 - any transport failure is reported, never raised.
        return unavailable_quota(
            redact_copilot_secrets(f"copilot quota request failed: {exc}", extra=secrets),
            fetched_at=fetched_at,
        )
    return parse_copilot_quota(payload, fetched_at=fetched_at)


class CopilotQuotaCache:
    """Remembers the last quota answer per credential for a minute."""

    def __init__(self, *, ttl_seconds: float = QUOTA_CACHE_TTL_SECONDS, failure_ttl_seconds: float = QUOTA_FAILURE_CACHE_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self.failure_ttl_seconds = failure_ttl_seconds
        self._lock = asyncio.Lock()
        self._cached: tuple[str, float, dict[str, Any]] | None = None

    def invalidate(self) -> None:
        self._cached = None

    def _fresh(self, fingerprint: str) -> dict[str, Any] | None:
        if self._cached is None:
            return None
        cached_fingerprint, stored_at, payload = self._cached
        if cached_fingerprint != fingerprint:
            return None
        ttl = self.ttl_seconds if payload.get("available") else self.failure_ttl_seconds
        if time.monotonic() - stored_at >= ttl:
            return None
        return payload

    async def get(self, settings: Settings, *, fetch: Any = fetch_copilot_quota) -> dict[str, Any]:
        credential = load_copilot_plugin_credential(settings)
        if credential is None:
            return unavailable_quota("copilot credential not configured")
        fingerprint = hashlib.sha256(credential.credential.encode("utf-8")).hexdigest()
        cached = self._fresh(fingerprint)
        if cached is not None:
            return dict(cached)
        async with self._lock:
            cached = self._fresh(fingerprint)
            if cached is not None:
                return dict(cached)
            payload = await fetch(settings, credential)
            self._cached = (fingerprint, time.monotonic(), payload)
            return dict(payload)


_DEFAULT_CACHE: CopilotQuotaCache | None = None


def _quota_cache(app: web.Application) -> CopilotQuotaCache:
    cache = app.get(COPILOT_QUOTA_CACHE_KEY)
    if isinstance(cache, CopilotQuotaCache):
        return cache
    global _DEFAULT_CACHE
    if _DEFAULT_CACHE is None:
        _DEFAULT_CACHE = CopilotQuotaCache()
    return _DEFAULT_CACHE


async def copilot_quota_handler(request: web.Request) -> web.Response:
    """``GET /api/copilot/quota``: the member's plan quota, or why it is unavailable (always 200)."""

    settings = request.app[SETTINGS_KEY]
    try:
        payload = await _quota_cache(request.app).get(settings)
    except Exception as exc:  # noqa: BLE001 - the endpoint never fails the Portal's status poll.
        payload = unavailable_quota(redact_copilot_secrets(f"copilot quota unavailable: {exc}"))
    return web.json_response(payload)


__all__ = [
    "QUOTA_CACHE_TTL_SECONDS",
    "QUOTA_PATH",
    "QUOTA_SOURCE",
    "CopilotQuotaCache",
    "copilot_quota_handler",
    "copilot_quota_url",
    "fetch_copilot_quota",
    "parse_copilot_quota",
    "unavailable_quota",
]
