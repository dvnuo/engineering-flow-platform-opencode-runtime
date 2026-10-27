from __future__ import annotations

import ipaddress
from typing import Mapping

import aiohttp
from aiohttp import web

from .app_keys import COPILOT_AUTO_SESSION_MANAGER_KEY, COPILOT_TOKEN_MANAGER_KEY, SETTINGS_KEY
from .copilot_plugin_auth import (
    COPILOT_PLUGIN_HEADERS,
    CopilotCredentialMissing,
    CopilotTokenExchangeError,
    redact_copilot_secrets,
)
from .copilot_auto_session import CopilotAutoSessionManager, route_auto_request_body
from .outbound_proxy import outbound_proxy_config_for_url


_REQUEST_HEADER_BLOCKLIST = {
    "host",
    "content-length",
    "authorization",
    "x-api-key",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_RESPONSE_HEADER_BLOCKLIST = _REQUEST_HEADER_BLOCKLIST | {
    "set-cookie",
    "www-authenticate",
    "content-encoding",
}


def _is_loopback_host(value: str | None) -> bool:
    host = str(value or "").strip().strip("[]")
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    if "%" in host:
        host = host.split("%", 1)[0]
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _request_is_loopback(request: web.Request) -> bool:
    if not _is_loopback_host(request.remote):
        return False
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        first_hop = forwarded_for.split(",", 1)[0].strip()
        if first_hop and not _is_loopback_host(first_hop):
            return False
    return True


_INITIATORS = frozenset({"user", "agent"})


def _inbound_initiator(headers: Mapping[str, str]) -> str | None:
    """OpenCode's own ``x-initiator`` (``user`` or ``agent``), or None when absent or not one of the two."""

    for key, value in headers.items():
        if str(key).lower() != "x-initiator":
            continue
        normalized = str(value or "").strip().lower()
        return normalized if normalized in _INITIATORS else None
    return None


def _safe_request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    outbound: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in _REQUEST_HEADER_BLOCKLIST:
            continue
        outbound[key] = value
    return outbound


def _safe_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered in _RESPONSE_HEADER_BLOCKLIST:
            continue
        if "token" in lowered or "secret" in lowered or "authorization" in lowered:
            continue
        safe[key] = redact_copilot_secrets(str(value))
    return safe


def _json_error(status: int, error: str, detail: str | None = None, *, extra: list[str] | None = None) -> web.Response:
    payload = {"success": False, "error": error}
    if detail:
        payload["detail"] = redact_copilot_secrets(detail, extra=extra or [])
    return web.json_response(payload, status=status)


def _upstream_url(base_url: str, tail: str, query_string: str) -> str:
    path = str(tail or "").lstrip("/")
    url = f"{base_url.rstrip('/')}/{path}" if path else base_url.rstrip("/")
    if query_string:
        url = f"{url}?{query_string}"
    return url


_DEFAULT_AUTO_SESSION_MANAGER: CopilotAutoSessionManager | None = None


def _auto_session_manager(app: web.Application) -> CopilotAutoSessionManager:
    manager = app.get(COPILOT_AUTO_SESSION_MANAGER_KEY)
    if isinstance(manager, CopilotAutoSessionManager):
        return manager
    global _DEFAULT_AUTO_SESSION_MANAGER
    if _DEFAULT_AUTO_SESSION_MANAGER is None:
        _DEFAULT_AUTO_SESSION_MANAGER = CopilotAutoSessionManager()
    return _DEFAULT_AUTO_SESSION_MANAGER


async def copilot_proxy_handler(request: web.Request) -> web.StreamResponse:
    if not _request_is_loopback(request):
        return _json_error(403, "forbidden")
    if request.method == "OPTIONS":
        return web.Response(status=204)
    if request.method not in {"GET", "POST"}:
        return _json_error(405, "method not allowed")

    token_manager = request.app[COPILOT_TOKEN_MANAGER_KEY]
    try:
        internal_token = await token_manager.get_token()
    except CopilotCredentialMissing:
        return _json_error(401, "copilot credential not configured")
    except CopilotTokenExchangeError as exc:
        return _json_error(502, "copilot token exchange failed", str(exc))
    except Exception as exc:
        return _json_error(502, "copilot proxy unavailable", str(exc))

    upstream = _upstream_url(
        internal_token.api_base_url,
        request.match_info.get("tail", ""),
        request.query_string,
    )
    settings = request.app[SETTINGS_KEY]
    proxy_config = outbound_proxy_config_for_url(settings, upstream)
    outbound_headers = _safe_request_headers(request.headers)
    inbound_initiator = _inbound_initiator(request.headers)
    for key in [key for key in outbound_headers if key.lower() == "x-initiator"]:
        outbound_headers.pop(key)
    outbound_headers.update(COPILOT_PLUGIN_HEADERS)
    outbound_headers.update(
        {
            "Authorization": f"Bearer {internal_token.token}",
            "Openai-Intent": "conversation-edits",
            # OpenCode labels the first model call of a member's turn ``user``
            # and the tool-loop continuations ``agent`` (VS Code's semantics);
            # the proxy passes that through and only labels a request that
            # says nothing itself. Billing is per token on AI credits, so the
            # label is about matching VS Code's traffic shape, not the bill.
            "x-initiator": inbound_initiator or "agent",
        }
    )
    body = await request.read()
    if request.method == "POST" and body:
        # Copilot "auto" is routed here the way VS Code does it: the body's
        # model becomes the model Copilot picked for the turn (POST /auto) and
        # the session token rides along (see copilot_auto_session). Other
        # bodies pass through byte for byte.
        routed = await route_auto_request_body(
            _auto_session_manager(request.app),
            body,
            internal_token,
            proxy_config=proxy_config,
        )
        if routed is not None:
            body, routing = routed
            if routing.session_token:
                outbound_headers["Copilot-Session-Token"] = routing.session_token

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
                    response_body = redact_copilot_secrets(
                        response_body.decode("utf-8", errors="replace"),
                        extra=[internal_token.token],
                    ).encode("utf-8")
                return web.Response(status=upstream_response.status, body=response_body, headers=response_headers)
    except Exception as exc:
        extra = [internal_token.token]
        if proxy_config.proxy_url:
            extra.append(proxy_config.proxy_url)
        return _json_error(502, "copilot upstream request failed", str(exc), extra=extra)
