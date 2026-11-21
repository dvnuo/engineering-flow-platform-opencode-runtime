"""The Portal trust token: who the adapter believes, and who it serves at all."""

import logging
import re

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter import portal_trust
from efp_opencode_adapter.chat_api import portal_author_metadata_from_request
from efp_opencode_adapter.logging_setup import request_logging_middleware
from efp_opencode_adapter.portal_trust import (
    PORTAL_TRUST_OPEN_PATHS,
    is_loopback_request,
    is_trusted_portal_request,
    log_portal_trust_status,
    portal_trust_middleware,
)
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient

TOKEN_ENV = "EFP_PORTAL_TRUST_TOKEN"
TOKEN_HEADER = "X-Portal-Trust-Token"
TOKEN = "3f9c0d5e7a1b4c6d8e2f0a1b3c5d7e9f"
PORTAL = {"X-Portal-Author-Source": "portal"}
IDENTITY = {**PORTAL, "X-Portal-User-Id": "user-2", "X-Portal-User-Name": "Bob"}
REMOTE_PEER = "10.42.3.17"
REFUSED = "portal_trust_token_required"


class _Transport:
    def __init__(self, peername):
        self._peername = peername

    def get_extra_info(self, key):
        return self._peername if key == "peername" else None


class _Request:
    def __init__(self, headers=None, peername=("127.0.0.1", 50000)):
        self.headers = headers or {}
        self.transport = _Transport(peername) if peername != "no-transport" else None


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    return TOKEN


@pytest.fixture
def no_token(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)


@pytest.fixture
def remote_peer(monkeypatch):
    """The test client's requests look as if another pod had sent them."""
    monkeypatch.setattr(portal_trust, "_peer_host", lambda request: REMOTE_PEER)


# --- A. Who counts as the Portal -------------------------------------------------


def test_without_a_token_the_author_source_header_alone_is_believed(no_token):
    assert is_trusted_portal_request(_Request(PORTAL)) is True
    assert is_trusted_portal_request(_Request({"X-Portal-Author-Source": " Portal "})) is True
    assert is_trusted_portal_request(_Request({})) is False
    assert is_trusted_portal_request(_Request({"X-Portal-Author-Source": "runtime"})) is False
    assert portal_author_metadata_from_request(_Request(IDENTITY))["author_id"] == "user-2"


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_token_variable_counts_as_not_set(monkeypatch, value):
    monkeypatch.setenv(TOKEN_ENV, value)
    assert is_trusted_portal_request(_Request(PORTAL)) is True


def test_with_a_token_the_portal_must_send_it(token):
    assert is_trusted_portal_request(_Request({**PORTAL, TOKEN_HEADER: TOKEN})) is True
    assert is_trusted_portal_request(_Request({**PORTAL, TOKEN_HEADER: f" {TOKEN} "})) is True
    assert portal_author_metadata_from_request(_Request({**IDENTITY, TOKEN_HEADER: TOKEN})) == {
        "author_type": "human",
        "author_source": "portal",
        "author_id": "user-2",
        "author_name": "Bob",
    }


@pytest.mark.parametrize(
    "headers",
    [
        IDENTITY,
        {**IDENTITY, TOKEN_HEADER: ""},
        {**IDENTITY, TOKEN_HEADER: "wrong"},
        {**IDENTITY, TOKEN_HEADER: TOKEN[:-1]},
        {**IDENTITY, TOKEN_HEADER: TOKEN + "0"},
        {**IDENTITY, TOKEN_HEADER: TOKEN.upper()},
        {**IDENTITY, TOKEN_HEADER: "t\u00f6ken"},
        {**IDENTITY, TOKEN_HEADER: "\udcff"},
        # The token without the author-source header is not a Portal request.
        {"X-Portal-User-Id": "user-2", TOKEN_HEADER: TOKEN},
        {**IDENTITY, "X-Portal-Author-Source": "runtime", TOKEN_HEADER: TOKEN},
    ],
)
def test_with_a_token_a_missing_or_wrong_one_is_not_the_portal(token, headers):
    assert is_trusted_portal_request(_Request(headers)) is False
    assert portal_author_metadata_from_request(_Request(headers)) == {}


def test_the_token_is_compared_in_constant_time(token, monkeypatch):
    seen = []

    def _compare(left, right):
        seen.append((left, right))
        return left == right

    monkeypatch.setattr(portal_trust.hmac, "compare_digest", _compare)
    assert is_trusted_portal_request(_Request({**PORTAL, TOKEN_HEADER: "wrong"})) is False
    assert seen == [(b"wrong", TOKEN.encode("utf-8"))]


# --- B. Who is served at all -----------------------------------------------------


@pytest.mark.parametrize(
    "peername",
    [("127.0.0.1", 50000), ("::1", 50000, 0, 0), ("::ffff:127.0.0.1", 50000, 0, 0)],
)
def test_the_pods_own_processes_are_loopback(peername):
    assert is_loopback_request(_Request(peername=peername)) is True


@pytest.mark.parametrize(
    "peername",
    [
        ("10.42.3.17", 50000),
        ("192.168.1.10", 50000),
        ("fd00::17", 50000, 0, 0),
        # What a service-mesh sidecar delivers other pods' traffic from.
        ("127.0.0.6", 50000),
        ("127.1.2.3", 50000),
        ("localhost", 50000),
        ("", 50000),
        (),
        None,
        "/run/adapter.sock",
        "no-transport",
    ],
)
def test_any_other_peer_is_not_loopback(peername):
    assert is_loopback_request(_Request(peername=peername)) is False


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Forwarded-For": "127.0.0.1"},
        {"X-Forwarded-For": "10.42.3.17, 127.0.0.1"},
        {"Forwarded": "for=127.0.0.1"},
        {"X-Real-IP": "127.0.0.1"},
    ],
)
def test_a_forwarded_header_never_makes_a_request_loopback(headers):
    # From another pod: the header is not read as the caller's address.
    assert is_loopback_request(_Request(headers, peername=("10.42.3.17", 50000))) is False
    # From a proxy inside the pod: the request is somebody else's.
    assert is_loopback_request(_Request(headers, peername=("127.0.0.1", 50000))) is False


def _app(calls=None):
    calls = calls if calls is not None else []

    async def handler(request):
        calls.append(request.path)
        return web.json_response({"ok": True, "loopback": is_loopback_request(request)})

    app = web.Application(middlewares=[request_logging_middleware, portal_trust_middleware])
    for path in (*sorted(PORTAL_TRUST_OPEN_PATHS), "/api/chat", "/healthz", "/ready/extra", "/api/health"):
        app.router.add_route("*", path, handler)
    return app


async def _started(app) -> TestClient:
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_the_peer_address_comes_from_the_socket(no_token):
    seen = {}

    async def handler(request):
        seen["peer"] = portal_trust._peer_host(request)
        seen["loopback"] = is_loopback_request(request)
        return web.json_response({})

    app = web.Application()
    app.router.add_get("/peer", handler)
    client = await _started(app)
    try:
        assert (await client.get("/peer")).status == 200
    finally:
        await client.close()
    assert seen == {"peer": "127.0.0.1", "loopback": True}


@pytest.mark.asyncio
async def test_without_a_token_every_caller_is_served(no_token, remote_peer):
    calls = []
    client = await _started(_app(calls))
    try:
        assert (await client.post("/api/chat", json={})).status == 200
        assert (await client.post("/api/chat", json={}, headers={TOKEN_HEADER: "anything"})).status == 200
    finally:
        await client.close()
    assert calls == ["/api/chat", "/api/chat"]


@pytest.mark.asyncio
async def test_with_a_token_a_request_without_it_is_refused(token, remote_peer, caplog):
    calls = []
    client = await _started(_app(calls))
    try:
        with caplog.at_level(logging.INFO):
            missing = await client.post("/api/chat", json={}, headers=PORTAL)
            wrong = await client.post("/api/chat", json={}, headers={**PORTAL, TOKEN_HEADER: "wrong"})
        assert missing.status == 401
        assert wrong.status == 401
        assert missing.content_type == "application/json"
        assert (await missing.json())["error"] == REFUSED
        assert (await wrong.json())["error"] == REFUSED
    finally:
        await client.close()
    assert calls == []
    rejected = [record.getMessage() for record in caplog.records if "portal_trust.rejected" in record.getMessage()]
    assert len(rejected) == 2
    assert "reason=missing_token" in rejected[0] and f"remote={REMOTE_PEER}" in rejected[0]
    assert "reason=wrong_token" in rejected[1]
    # The request log wraps the trust check, so the refusal is on record.
    assert [line for line in (r.getMessage() for r in caplog.records) if line.startswith("http.end") and "status=401" in line]
    assert TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_with_a_token_the_portal_is_served(token, remote_peer):
    calls = []
    client = await _started(_app(calls))
    try:
        # The token alone is enough to be served: the Portal's task dispatcher
        # sends no author-source header.
        assert (await client.post("/api/chat", json={}, headers={TOKEN_HEADER: TOKEN})).status == 200
        assert (await client.get("/api/chat", headers={**PORTAL, TOKEN_HEADER: TOKEN})).status == 200
    finally:
        await client.close()
    assert calls == ["/api/chat", "/api/chat"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", sorted(PORTAL_TRUST_OPEN_PATHS))
async def test_probe_endpoints_are_served_without_the_token(token, remote_peer, path):
    client = await _started(_app())
    try:
        assert (await client.get(path)).status == 200
        assert (await client.get(path, headers={TOKEN_HEADER: "wrong"})).status == 200
    finally:
        await client.close()


def test_the_open_paths_are_the_probe_endpoints_only():
    assert PORTAL_TRUST_OPEN_PATHS == {"/health", "/actuator/health", "/ready"}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/healthz", "/ready/extra", "/api/health", "/health/", "/HEALTH", "/health/../api/chat"])
async def test_a_path_that_only_looks_like_a_probe_is_refused(token, remote_peer, path):
    calls = []
    client = await _started(_app(calls))
    try:
        assert (await client.get(path)).status == 401
    finally:
        await client.close()
    assert calls == []


@pytest.mark.asyncio
async def test_callers_inside_the_pod_are_served_without_the_token(token):
    # The test client connects from 127.0.0.1, as the OpenCode child does.
    calls = []
    client = await _started(_app(calls))
    try:
        response = await client.post("/api/chat", json={})
        assert response.status == 200
        assert (await response.json())["loopback"] is True
    finally:
        await client.close()
    assert calls == ["/api/chat"]


@pytest.mark.asyncio
@pytest.mark.parametrize("header", ["X-Forwarded-For", "Forwarded", "X-Real-IP"])
async def test_a_request_relayed_through_loopback_needs_the_token(token, header):
    calls = []
    client = await _started(_app(calls))
    try:
        assert (await client.post("/api/chat", json={}, headers={header: "127.0.0.1"})).status == 401
        assert (await client.post("/api/chat", json={}, headers={header: "127.0.0.1", TOKEN_HEADER: TOKEN})).status == 200
    finally:
        await client.close()
    assert calls == ["/api/chat"]


@pytest.mark.asyncio
async def test_a_forwarded_header_does_not_open_the_door_from_another_pod(token, remote_peer):
    calls = []
    client = await _started(_app(calls))
    try:
        for header in ("X-Forwarded-For", "Forwarded", "X-Real-IP"):
            assert (await client.post("/api/chat", json={}, headers={header: "127.0.0.1"})).status == 401
    finally:
        await client.close()
    assert calls == []


# --- The real application --------------------------------------------------------


def _adapter_app(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    return create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())


def test_the_adapter_runs_the_trust_check_before_every_handler(tmp_path, monkeypatch):
    middlewares = list(_adapter_app(tmp_path, monkeypatch).middlewares)
    assert portal_trust_middleware in middlewares
    # The request log stays outermost, so a refused request is still logged.
    assert middlewares.index(request_logging_middleware) < middlewares.index(portal_trust_middleware)


@pytest.mark.asyncio
async def test_every_adapter_route_is_closed_to_a_caller_without_the_token(tmp_path, monkeypatch, token, remote_peer):
    app = _adapter_app(tmp_path, monkeypatch)
    targets = sorted(
        {
            ("POST" if route.method == "*" else route.method, re.sub(r"\{[^}]+\}", "x", route.resource.canonical))
            for route in app.router.routes()
            if route.resource is not None
        }
    )
    assert len(targets) > 50
    assert ("POST", "/api/chat") in targets
    assert ("GET", "/api/events") in targets
    assert ("GET", "/api/server-files/read") in targets
    assert ("POST", "/api/internal/copilot/x") in targets
    assert ("GET", "/api/internal/opencode/log-tail") in targets

    open_paths = set()
    client = await _started(app)
    try:
        for method, path in targets:
            response = await client.request(method, path, headers=IDENTITY)
            refused = response.status == 401 and (method == "HEAD" or (await response.json()).get("error") == REFUSED)
            if not refused:
                open_paths.add(path)
            response.release()
        # A path no route serves is refused too, not answered with 404.
        unknown = await client.get("/api/no-such-route")
        assert unknown.status == 401
    finally:
        await client.close()

    assert open_paths == set(PORTAL_TRUST_OPEN_PATHS)


@pytest.mark.asyncio
async def test_adapter_event_stream_with_and_without_the_token(tmp_path, monkeypatch, token, remote_peer):
    client = await _started(_adapter_app(tmp_path, monkeypatch))
    try:
        # The WebSocket handshake is refused before the upgrade.
        with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
            await client.ws_connect("/api/events")
        assert refused.value.status == 401
        with pytest.raises(aiohttp.WSServerHandshakeError) as wrong:
            await client.ws_connect("/api/events", headers={**PORTAL, TOKEN_HEADER: "wrong"})
        assert wrong.value.status == 401

        socket = await client.ws_connect("/api/events", headers={**PORTAL, TOKEN_HEADER: TOKEN})
        assert await socket.receive_json(timeout=5) == {"type": "connected", "engine": "opencode"}
        await socket.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_the_opencode_child_still_reaches_the_model_proxies(tmp_path, monkeypatch, token):
    # No token and no Portal headers, from 127.0.0.1: the request reaches the
    # proxy handler, which answers for itself (no Copilot credential here).
    client = await _started(_adapter_app(tmp_path, monkeypatch))
    try:
        response = await client.post("/api/internal/copilot/chat/completions", json={})
        payload = await response.json()
        assert payload.get("error") != REFUSED
        assert payload["error"] == "copilot credential not configured"
    finally:
        await client.close()


# --- Startup ---------------------------------------------------------------------


def _trust_records(caplog, level):
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "efp_opencode_adapter.portal_trust" and record.levelno == level
    ]


def test_startup_warns_when_portal_requests_are_not_authenticated(no_token, caplog):
    with caplog.at_level(logging.INFO, logger="efp_opencode_adapter.portal_trust"):
        log_portal_trust_status()
    warnings = _trust_records(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert TOKEN_ENV in warnings[0]
    assert "not authenticated" in warnings[0]


def test_startup_does_not_warn_or_print_the_token_when_it_is_set(token, caplog):
    with caplog.at_level(logging.INFO, logger="efp_opencode_adapter.portal_trust"):
        log_portal_trust_status()
    assert _trust_records(caplog, logging.WARNING) == []
    assert len(_trust_records(caplog, logging.INFO)) == 1
    assert TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_the_adapter_reports_the_trust_status_once_at_startup(tmp_path, monkeypatch, no_token, caplog):
    with caplog.at_level(logging.WARNING, logger="efp_opencode_adapter.portal_trust"):
        client = await _started(_adapter_app(tmp_path, monkeypatch))
        try:
            assert (await client.get("/api/capabilities")).status == 200
            assert (await client.get("/api/capabilities")).status == 200
        finally:
            await client.close()
    assert len(_trust_records(caplog, logging.WARNING)) == 1


def test_the_model_proxies_never_pass_the_trust_token_on():
    # A header of this adapter alone: the proxies copy a caller's headers to
    # GitHub and the AI Platform, and this one must not be among them.
    from efp_opencode_adapter.copilot_proxy import _safe_request_headers

    kept = _safe_request_headers({"X-Portal-Trust-Token": "secret", "x-portal-trust-token": "secret", "X-Initiator": "agent"})
    assert kept == {"X-Initiator": "agent"}
