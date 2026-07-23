"""Adapter-side logging: stdout handler, profile-driven level, request lines.

Everything here writes to STDOUT because that is what ``kubectl logs`` reads.
The level honours the runtime profile's debug settings (projected as
EFP_DEBUG / LOG_LEVEL for the managed child) so turning debug on in a profile
also turns it on for the adapter itself.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import uuid
from typing import Mapping

from aiohttp import web

logger = logging.getLogger(__name__)

# Inbound correlation headers, most specific first. aiohttp header lookup is
# case-insensitive, so one spelling per header is enough.
REQUEST_ID_HEADERS = ("X-Request-Id", "X-Correlation-Id", "X-Trace-Id")

# Typed key like app_keys.py, degrading to a plain string on aiohttp builds
# that predate web.RequestKey (pyproject allows aiohttp>=3.9).
_REQUEST_KEY_TYPE = getattr(web, "RequestKey", None)
REQUEST_ID_KEY = _REQUEST_KEY_TYPE("efp_request_id", str) if _REQUEST_KEY_TYPE else "efp_request_id"
MAX_REQUEST_ID_CHARS = 200

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_HANDLER_MARKER = "_efp_stdout_handler"
_FALSEY = {"0", "false", "no", "off", ""}

# Kubernetes probes and Spring-actuator scanners are the overwhelming majority
# of inbound requests (96.8% of the access lines in a 25-minute production
# sample), which buries the handful of real API calls. Their access lines drop
# to DEBUG so they stay available when debug logging is on.
PROBE_PATHS = frozenset({"/ready", "/readyz", "/health", "/healthz", "/live"})
ACTUATOR_PATH_PREFIX = "/actuator"


def is_probe_path(path: str) -> bool:
    """True for probe/scanner paths whose access lines belong at DEBUG.

    Matches the probe paths exactly and anything under /actuator (including
    bare /actuator); a path that merely contains the word is a real request.
    """
    normalized = str(path or "").rstrip("/") or "/"
    if normalized in PROBE_PATHS:
        return True
    return normalized == ACTUATOR_PATH_PREFIX or normalized.startswith(ACTUATOR_PATH_PREFIX + "/")


def resolve_log_level(env: Mapping[str, str] | None = None) -> int:
    """EFP_LOG_LEVEL, then LOG_LEVEL, then EFP_DEBUG=1 => DEBUG, else INFO."""
    source = os.environ if env is None else env
    for key in ("EFP_LOG_LEVEL", "LOG_LEVEL"):
        raw = str(source.get(key) or "").strip().upper()
        if not raw:
            continue
        level = logging.getLevelName(raw)
        if isinstance(level, int):
            return level
    if str(source.get("EFP_DEBUG") or "").strip().lower() not in _FALSEY:
        return logging.DEBUG
    return logging.INFO


def configure_logging(env: Mapping[str, str] | None = None) -> int:
    """Install (or re-level) a single stdout handler on the root logger."""
    level = resolve_log_level(env)
    root = logging.getLogger()
    root.setLevel(level)
    for existing in root.handlers:
        if getattr(existing, _HANDLER_MARKER, False):
            existing.setLevel(level)
            break
    else:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        setattr(handler, _HANDLER_MARKER, True)
        root.addHandler(handler)
    logger.info("adapter.logging.configured level=%s", logging.getLevelName(level))
    return level


def _field(value: object) -> str:
    """One-token log value: no whitespace, never empty."""
    text = "".join("_" if ch.isspace() else ch for ch in str(value or ""))
    return text or "-"


def resolve_request_id(request: web.Request) -> str:
    for header in REQUEST_ID_HEADERS:
        value = (request.headers.get(header) or "").strip()
        if value:
            return value[:MAX_REQUEST_ID_CHARS]
    return uuid.uuid4().hex


@web.middleware
async def request_logging_middleware(request: web.Request, handler):
    """One stdout line per request in, one per request out (always).

    Probe traffic is demoted to DEBUG only while it succeeds. ``/ready`` and
    ``/health`` report failure by *returning* 503 rather than raising, and
    aiohttp's own access log is disabled because this middleware replaces it
    (``access_log=None`` on both the AppRunner and run_app), so keying the
    level on the path alone would leave a pod stuck failing readiness with
    nothing at INFO to explain why -- the single most important signal when a
    pod will not come up.
    """
    request_id = resolve_request_id(request)
    request[REQUEST_ID_KEY] = request_id
    method = _field(request.method)
    path = _field(request.path)
    started = time.monotonic()
    status: object = 500
    is_probe = is_probe_path(request.path)
    level = logging.DEBUG if is_probe else logging.INFO
    logger.log(level, "http.start method=%s path=%s request_id=%s", method, path, _field(request_id))
    try:
        response = await handler(request)
        status = getattr(response, "status", 0) or 0
        return response
    except web.HTTPException as exc:
        status = exc.status
        raise
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    finally:
        duration_ms = (time.monotonic() - started) * 1000.0
        # Only a *successful* probe stays quiet; a failing one is the line the
        # operator most needs. Non-int statuses (the "cancelled" sentinel, the
        # 500 default) count as not-success.
        succeeded = isinstance(status, int) and 0 < status < 400
        end_level = logging.DEBUG if (is_probe and succeeded) else logging.INFO
        logger.log(
            end_level,
            "http.end method=%s path=%s status=%s duration_ms=%.1f request_id=%s",
            method,
            path,
            _field(status),
            duration_ms,
            _field(request_id),
        )
