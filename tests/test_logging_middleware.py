import logging

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.logging_setup import (
    REQUEST_ID_KEY,
    configure_logging,
    is_probe_path,
    request_logging_middleware,
    resolve_log_level,
)
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient


def _lines(caplog, prefix):
    return [record.getMessage() for record in caplog.records if record.getMessage().startswith(prefix)]


def _http_records(caplog, level):
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == level and record.getMessage().startswith("http.")
    ]


def _build_app(handler, path="/probe", method="GET"):
    app = web.Application(middlewares=[request_logging_middleware])
    app.router.add_route(method, path, handler)
    return app


@pytest.mark.asyncio
async def test_middleware_logs_start_and_end_lines_with_duration(caplog):
    async def handler(request):
        return web.json_response({"ok": True})

    client = TestClient(TestServer(_build_app(handler)))
    await client.start_server()
    try:
        with caplog.at_level(logging.INFO, logger="efp_opencode_adapter.logging_setup"):
            response = await client.get("/probe")
    finally:
        await client.close()

    assert response.status == 200
    start_lines = _lines(caplog, "http.start")
    end_lines = _lines(caplog, "http.end")
    assert len(start_lines) == 1 and len(end_lines) == 1
    assert "method=GET" in start_lines[0] and "path=/probe" in start_lines[0]
    assert "request_id=" in start_lines[0]
    assert "method=GET" in end_lines[0] and "path=/probe" in end_lines[0]
    assert "status=200" in end_lines[0]
    assert "duration_ms=" in end_lines[0]
    # one line each, greppable key=value
    assert "\n" not in start_lines[0] and "\n" not in end_lines[0]


@pytest.mark.asyncio
async def test_middleware_reuses_inbound_request_id_header(caplog):
    seen = {}

    async def handler(request):
        seen["request_id"] = request[REQUEST_ID_KEY]
        return web.Response(text="ok")

    client = TestClient(TestServer(_build_app(handler)))
    await client.start_server()
    try:
        with caplog.at_level(logging.INFO, logger="efp_opencode_adapter.logging_setup"):
            await client.get("/probe", headers={"X-Request-Id": "portal-req-42"})
    finally:
        await client.close()

    assert seen["request_id"] == "portal-req-42"
    assert "request_id=portal-req-42" in _lines(caplog, "http.start")[0]
    assert "request_id=portal-req-42" in _lines(caplog, "http.end")[0]


@pytest.mark.asyncio
async def test_middleware_logs_end_line_when_handler_raises(caplog):
    async def handler(request):
        raise web.HTTPBadGateway(text="boom")

    client = TestClient(TestServer(_build_app(handler)))
    await client.start_server()
    try:
        with caplog.at_level(logging.INFO, logger="efp_opencode_adapter.logging_setup"):
            response = await client.get("/probe")
    finally:
        await client.close()

    assert response.status == 502
    assert "status=502" in _lines(caplog, "http.end")[0]


async def _get_and_capture(caplog, path):
    async def handler(request):
        return web.json_response({"ok": True})

    client = TestClient(TestServer(_build_app(handler, path=path)))
    await client.start_server()
    try:
        with caplog.at_level(logging.DEBUG, logger="efp_opencode_adapter.logging_setup"):
            await client.get(path)
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["/ready", "/readyz", "/health", "/healthz", "/live", "/actuator", "/actuator/health", "/actuator/threaddump"],
)
async def test_probe_requests_are_logged_at_debug_and_never_at_info(caplog, path):
    await _get_and_capture(caplog, path)

    assert _http_records(caplog, logging.INFO) == []
    debug_lines = _http_records(caplog, logging.DEBUG)
    assert len(debug_lines) == 2
    assert debug_lines[0].startswith("http.start") and f"path={path}" in debug_lines[0]
    assert debug_lines[1].startswith("http.end") and "status=200" in debug_lines[1]


@pytest.mark.asyncio
async def test_failing_probe_end_line_resurfaces_at_info(caplog):
    """A probe demoted to DEBUG must return to INFO when it FAILS.

    /ready and /health report failure by returning 503, not by raising, and
    this middleware replaces aiohttp's own access log (access_log=None), so a
    pod stuck failing readiness would otherwise emit nothing at INFO.
    """
    async def handler(request):
        return web.json_response({"ready": False}, status=503)

    client = TestClient(TestServer(_build_app(handler, path="/ready")))
    await client.start_server()
    try:
        with caplog.at_level(logging.DEBUG, logger="efp_opencode_adapter.logging_setup"):
            await client.get("/ready")
    finally:
        await client.close()

    info_lines = _http_records(caplog, logging.INFO)
    assert any(l.startswith("http.end") and "status=503" in l for l in info_lines)
    assert not any(l.startswith("http.end") for l in _http_records(caplog, logging.DEBUG))


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/chat/stream", "/api/sessions", "/api/actuator-report", "/myactuator"])
async def test_real_requests_stay_at_info(caplog, path):
    await _get_and_capture(caplog, path)

    info_lines = _http_records(caplog, logging.INFO)
    assert len(info_lines) == 2
    assert info_lines[0].startswith("http.start") and f"path={path}" in info_lines[0]
    assert info_lines[1].startswith("http.end")
    assert _http_records(caplog, logging.DEBUG) == []


def test_is_probe_path_matches_probes_only():
    assert is_probe_path("/ready") and is_probe_path("/health") and is_probe_path("/live")
    assert is_probe_path("/healthz") and is_probe_path("/readyz")
    assert is_probe_path("/actuator") and is_probe_path("/actuator/") and is_probe_path("/actuator/env")
    assert not is_probe_path("/api/actuator-report")
    assert not is_probe_path("/myactuator")
    assert not is_probe_path("/api/health-report")
    assert not is_probe_path("/api/chat/stream")
    assert not is_probe_path("/")


def test_create_app_wires_the_request_logging_middleware(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    assert request_logging_middleware in app.middlewares


def test_log_level_prefers_efp_log_level_then_log_level_then_debug():
    assert resolve_log_level({"EFP_LOG_LEVEL": "warning", "LOG_LEVEL": "debug", "EFP_DEBUG": "1"}) == logging.WARNING
    assert resolve_log_level({"LOG_LEVEL": "DEBUG"}) == logging.DEBUG
    assert resolve_log_level({"EFP_DEBUG": "1"}) == logging.DEBUG
    assert resolve_log_level({"EFP_DEBUG": "0"}) == logging.INFO
    assert resolve_log_level({}) == logging.INFO
    # An unparseable level must not break boot.
    assert resolve_log_level({"LOG_LEVEL": "chatty"}) == logging.INFO


def test_configure_logging_installs_one_stdout_handler_and_applies_level():
    import sys

    root = logging.getLogger()
    original_level = root.level
    original_handlers = list(root.handlers)
    try:
        assert configure_logging({"EFP_LOG_LEVEL": "DEBUG"}) == logging.DEBUG
        assert configure_logging({"EFP_LOG_LEVEL": "DEBUG"}) == logging.DEBUG
        managed = [h for h in root.handlers if getattr(h, "_efp_stdout_handler", False)]
        assert len(managed) == 1
        assert managed[0].stream is sys.stdout
        assert root.level == logging.DEBUG

        configure_logging({"LOG_LEVEL": "WARNING"})
        assert root.level == logging.WARNING
        assert managed[0].level == logging.WARNING
    finally:
        root.handlers = original_handlers
        root.setLevel(original_level)
