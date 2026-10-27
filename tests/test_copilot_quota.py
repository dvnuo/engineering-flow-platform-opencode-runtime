"""The member's Copilot plan quota (GET copilot_internal/user) behind /api/copilot/quota."""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter import copilot_quota as mod
from efp_opencode_adapter.app_keys import COPILOT_QUOTA_CACHE_KEY
from efp_opencode_adapter.copilot_plugin_auth import CopilotSourceCredential, save_copilot_plugin_credential
from efp_opencode_adapter.copilot_quota import CopilotQuotaCache, fetch_copilot_quota, parse_copilot_quota, unavailable_quota
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient

# What the community Copilot tools document for a Business seat: snapshots by
# quota id with entitlement/remaining/percent, a reset date and the plan.
SAMPLE = {
    "access_type_sku": "copilot_business_seat",
    "copilot_plan": "business",
    "chat_enabled": True,
    "quota_reset_date": "2026-11-01",
    "quota_snapshots": {
        "chat": {"entitlement": 0, "remaining": 0, "percent_remaining": 0, "unlimited": True, "overage_count": 0, "overage_permitted": False, "quota_id": "chat"},
        "completions": {"entitlement": 0, "remaining": 0, "percent_remaining": 0, "unlimited": True, "quota_id": "completions"},
        "premium_interactions": {"entitlement": 300, "remaining": 71.5, "percent_remaining": 23.83, "unlimited": False, "overage_count": 0, "overage_permitted": True, "quota_id": "premium_interactions", "quota_remaining": 71.5},
        "ai_credits": {"entitlement": 1900, "remaining": 412.5, "unlimited": False},
    },
}


def test_parse_reads_snapshots_plan_reset_date_and_credits():
    payload = parse_copilot_quota(SAMPLE, fetched_at="2026-10-27T00:00:00Z")
    assert payload["available"] is True
    assert payload["source"] == "copilot_internal_user"
    assert payload["fetched_at"] == "2026-10-27T00:00:00Z"
    assert payload["plan"] == "business"
    assert payload["reset_date"] == "2026-11-01"
    assert payload["error"] is None
    assert payload["raw_keys"] == ["access_type_sku", "chat_enabled", "copilot_plan", "quota_reset_date", "quota_snapshots"]
    by_id = {snapshot["id"]: snapshot for snapshot in payload["snapshots"]}
    assert set(by_id) == {"chat", "completions", "premium_interactions", "ai_credits"}
    premium = by_id["premium_interactions"]
    assert premium["label"] == "Premium requests"
    assert premium["entitlement"] == 300
    assert premium["remaining"] == 71.5
    assert premium["used"] == 228.5
    assert premium["percent_remaining"] == 23.83
    assert premium["unlimited"] is False
    assert premium["overage_permitted"] is True
    assert by_id["chat"]["unlimited"] is True
    assert by_id["chat"]["used"] is None
    credits = by_id["ai_credits"]
    assert credits["label"] == "AI credits"
    # percent_remaining is derived when the answer leaves it out.
    assert credits["percent_remaining"] == 21.7
    assert payload["credits"] == {"included": 1900, "used": 1487.5, "remaining": 412.5, "unlimited": False}


def test_parse_tolerates_list_shaped_snapshots_and_odd_payloads():
    listed = parse_copilot_quota(
        {"plan": "pro", "quota_snapshots": [{"quota_id": "premium_interactions", "entitlement": 300, "quota_remaining": 10}, "junk", {"name": "weird_thing", "unlimited": "yes"}]}
    )
    assert listed["available"] is True
    assert listed["plan"] == "pro"
    assert listed["credits"] is None
    assert [snapshot["id"] for snapshot in listed["snapshots"]] == ["premium_interactions", "weird_thing"]
    assert listed["snapshots"][0]["remaining"] == 10
    assert listed["snapshots"][0]["used"] == 290
    assert listed["snapshots"][1]["label"] == "Weird thing"
    assert listed["snapshots"][1]["unlimited"] is True

    empty = parse_copilot_quota({})
    assert empty["available"] is True and empty["snapshots"] == [] and empty["credits"] is None

    wrong = parse_copilot_quota(["not", "an", "object"])
    assert wrong["available"] is False
    assert "non-object" in wrong["error"]
    assert unavailable_quota("x")["available"] is False


async def _upstream(handler):
    app = web.Application()
    app.router.add_get("/copilot_internal/user", handler)
    server = TestServer(app)
    await server.start_server()
    return server


@pytest.mark.asyncio
async def test_fetch_sends_the_members_credential_with_the_plugin_headers(monkeypatch):
    captured = {}

    async def handler(request):
        captured["headers"] = dict(request.headers)
        return web.json_response(SAMPLE)

    server = await _upstream(handler)
    monkeypatch.setenv("EFP_COPILOT_GITHUB_API_BASE_URL", str(server.make_url("/")).rstrip("/"))
    payload = await fetch_copilot_quota(Settings.from_env(), CopilotSourceCredential(credential="gho_PORTAL", source="api_key"))

    assert payload["available"] is True
    assert payload["plan"] == "business"
    assert captured["headers"]["Authorization"] == "Bearer gho_PORTAL"
    assert captured["headers"]["Accept"] == "application/json"
    assert captured["headers"]["Copilot-Integration-Id"] == "vscode-chat"
    assert captured["headers"]["Editor-Version"] == "vscode/1.133.0"
    await server.close()


@pytest.mark.asyncio
async def test_fetch_failures_become_unavailable_payloads_not_exceptions(monkeypatch):
    async def denied(_request):
        return web.json_response({"message": "Bad credentials"}, status=401)

    server = await _upstream(denied)
    monkeypatch.setenv("EFP_COPILOT_GITHUB_API_BASE_URL", str(server.make_url("/")).rstrip("/"))
    settings = Settings.from_env()
    credential = CopilotSourceCredential(credential="gho_PORTAL", source="api_key")
    denied_payload = await fetch_copilot_quota(settings, credential)
    assert denied_payload["available"] is False
    assert denied_payload["error"] == "copilot quota request failed with HTTP 401"
    assert denied_payload["snapshots"] == []
    await server.close()

    async def garbage(_request):
        return web.Response(text="<html>nope</html>", content_type="text/html")

    server = await _upstream(garbage)
    monkeypatch.setenv("EFP_COPILOT_GITHUB_API_BASE_URL", str(server.make_url("/")).rstrip("/"))
    garbage_payload = await fetch_copilot_quota(Settings.from_env(), credential)
    assert garbage_payload["available"] is False
    assert garbage_payload["error"] == "copilot quota returned invalid json"
    await server.close()

    # Nothing listening: the transport error is reported, with the credential redacted.
    monkeypatch.setenv("EFP_COPILOT_GITHUB_API_BASE_URL", "http://127.0.0.1:9")
    down = await fetch_copilot_quota(Settings.from_env(), credential)
    assert down["available"] is False
    assert down["error"].startswith("copilot quota request failed:")
    assert "gho_PORTAL" not in down["error"]


@pytest.mark.asyncio
async def test_quota_endpoint_serves_the_cached_answer_and_reports_a_missing_credential(tmp_path, monkeypatch):
    calls = {"count": 0}

    async def handler(_request):
        calls["count"] += 1
        return web.json_response(SAMPLE)

    server = await _upstream(handler)
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_COPILOT_GITHUB_API_BASE_URL", str(server.make_url("/")).rstrip("/"))
    settings = Settings.from_env()
    app = create_app(settings, opencode_client=FakeOpenCodeClient())
    assert isinstance(app[COPILOT_QUOTA_CACHE_KEY], CopilotQuotaCache)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        missing = await (await client.get("/api/copilot/quota")).json()
        assert missing["available"] is False
        assert missing["error"] == "copilot credential not configured"
        assert calls["count"] == 0

        save_copilot_plugin_credential(settings, CopilotSourceCredential(credential="gho_PORTAL", source="api_key"))
        first_response = await client.get("/api/copilot/quota")
        first = await first_response.json()
        second = await (await client.get("/api/copilot/quota")).json()

        assert first_response.status == 200
        assert first["available"] is True
        assert first["credits"]["remaining"] == 412.5
        assert first["reset_date"] == "2026-11-01"
        assert second == first
        # Two reads inside the cache window cost GitHub one request.
        assert calls["count"] == 1

        app[COPILOT_QUOTA_CACHE_KEY].invalidate()
        await client.get("/api/copilot/quota")
        assert calls["count"] == 2
    finally:
        await client.close()
        await server.close()


@pytest.mark.asyncio
async def test_cache_keeps_failures_only_briefly_and_per_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    settings = Settings.from_env()
    save_copilot_plugin_credential(settings, CopilotSourceCredential(credential="gho_ONE", source="api_key"))
    answers = iter([unavailable_quota("boom"), parse_copilot_quota(SAMPLE), parse_copilot_quota({"copilot_plan": "other"})])
    fetched = []

    async def fake_fetch(_settings, credential):
        fetched.append(credential.credential)
        return next(answers)

    now = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    cache = CopilotQuotaCache(ttl_seconds=60, failure_ttl_seconds=15)

    assert (await cache.get(settings, fetch=fake_fetch))["available"] is False
    now[0] += 10
    assert (await cache.get(settings, fetch=fake_fetch))["available"] is False  # failure still cached
    now[0] += 10
    assert (await cache.get(settings, fetch=fake_fetch))["plan"] == "business"  # failure expired, refetched
    now[0] += 59
    assert (await cache.get(settings, fetch=fake_fetch))["plan"] == "business"  # success cached a minute
    save_copilot_plugin_credential(settings, CopilotSourceCredential(credential="gho_TWO", source="api_key"))
    assert (await cache.get(settings, fetch=fake_fetch))["plan"] == "other"  # another member, another answer
    assert fetched == ["gho_ONE", "gho_ONE", "gho_TWO"]
