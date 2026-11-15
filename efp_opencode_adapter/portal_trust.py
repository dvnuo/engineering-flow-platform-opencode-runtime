"""Who may call the adapter API: the Portal's trust token.

The Portal injects ``EFP_PORTAL_TRUST_TOKEN``, a secret of this one assistant,
into the pod and sends it as ``X-Portal-Trust-Token`` on every request it makes
to the adapter. While the variable is set:

* a request counts as the Portal's only if it carries the token next to
  ``X-Portal-Author-Source: portal`` (``is_trusted_portal_request``), and
* ``portal_trust_middleware`` answers 401 to every request without the token,
  except the probe endpoints and callers inside the pod (the OpenCode child
  reaches the adapter's model proxies on 127.0.0.1).

A pod created before the Portal injected the variable has none. The adapter
then behaves as it always did -- the author-source header alone is believed
and every caller is served -- and says so once at startup. The adapter sends
the same token on its own calls to the Portal (``portal_metadata_client``).
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

PORTAL_TRUST_TOKEN_ENV = "EFP_PORTAL_TRUST_TOKEN"
PORTAL_TRUST_TOKEN_HEADER = "X-Portal-Trust-Token"
PORTAL_AUTHOR_SOURCE_HEADER = "X-Portal-Author-Source"

# Served without the token. GET /ready is the kubelet's readiness probe and the
# Portal's own check of the applied profile revision; the two health paths are
# the liveness endpoints. Neither side of a probe can hold the token.
PORTAL_TRUST_OPEN_PATHS = frozenset({"/health", "/actuator/health", "/ready"})

# The pod's own processes reach the adapter on 127.0.0.1. Only these two
# addresses count, not the whole of 127.0.0.0/8: a service-mesh sidecar hands
# the traffic of other pods to the application from 127.0.0.6.
_LOOPBACK_ADDRESSES = frozenset({ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")})
# How a proxy says that a request is somebody else's. A request carrying one
# is never treated as the pod's own, whatever address it arrives from.
_FORWARDED_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Real-IP")


def portal_trust_token() -> str:
    """The Portal trust token of this pod; empty when the Portal injected none."""
    return str(os.getenv(PORTAL_TRUST_TOKEN_ENV) or "").strip()


def _secrets_match(presented: str, expected: str) -> bool:
    try:
        return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
    except UnicodeEncodeError:
        return False


def _carries_trust_token(request: Any, expected: str) -> bool:
    headers = getattr(request, "headers", {}) or {}
    presented = str(headers.get(PORTAL_TRUST_TOKEN_HEADER) or "").strip()
    return bool(presented) and _secrets_match(presented, expected)


def is_trusted_portal_request(request: Any) -> bool:
    """Whether the Portal made this request, so its identity headers are believed."""
    headers = getattr(request, "headers", {}) or {}
    source = str(headers.get(PORTAL_AUTHOR_SOURCE_HEADER) or "").strip().lower()
    if source != "portal":
        return False
    expected = portal_trust_token()
    if not expected:
        # A pod without the token: the header alone, as before.
        return True
    return _carries_trust_token(request, expected)


def _peer_host(request: Any) -> str:
    """The address at the other end of the socket; a header never changes it."""
    transport = getattr(request, "transport", None)
    if transport is None:
        return ""
    peername = transport.get_extra_info("peername")
    if isinstance(peername, (tuple, list)) and peername:
        return str(peername[0])
    return ""


def is_loopback_request(request: Any) -> bool:
    """Whether a process inside the pod made this request directly."""
    headers = getattr(request, "headers", {}) or {}
    if any(name in headers for name in _FORWARDED_HEADERS):
        return False
    try:
        address = ipaddress.ip_address(_peer_host(request).split("%", 1)[0])
    except ValueError:
        return False
    address = getattr(address, "ipv4_mapped", None) or address
    return address in _LOOPBACK_ADDRESSES


def _field(value: object) -> str:
    """One-token log value: printable, no whitespace, never empty."""
    text = "".join(ch if ch.isprintable() and not ch.isspace() else "_" for ch in str(value or ""))
    return text[:200] or "-"


@web.middleware
async def portal_trust_middleware(request: web.Request, handler):
    """Refuse every request that is not the Portal's, a probe, or the pod's own.

    Installed on the application, so it runs before any route's handler: a
    WebSocket handshake (``GET /api/events``) is refused the same way, with a
    401 in place of the upgrade.
    """
    expected = portal_trust_token()
    if (
        not expected
        or request.path in PORTAL_TRUST_OPEN_PATHS
        or is_loopback_request(request)
        or _carries_trust_token(request, expected)
    ):
        return await handler(request)
    logger.warning(
        "portal_trust.rejected method=%s path=%s remote=%s reason=%s",
        _field(request.method),
        _field(request.path),
        _field(_peer_host(request)),
        "wrong_token" if PORTAL_TRUST_TOKEN_HEADER in request.headers else "missing_token",
    )
    return web.json_response(
        {
            "error": "portal_trust_token_required",
            "message": "This runtime only serves requests that carry its Portal trust token.",
        },
        status=401,
    )


def log_portal_trust_status() -> None:
    """One line at startup saying whether Portal requests are authenticated."""
    if portal_trust_token():
        logger.info(
            "Portal trust token is set: requests without %s are refused, except probes and loopback callers",
            PORTAL_TRUST_TOKEN_HEADER,
        )
        return
    logger.warning(
        "%s is not set: Portal requests are not authenticated, and any caller that can reach this "
        "runtime is served and may claim to be the Portal",
        PORTAL_TRUST_TOKEN_ENV,
    )
