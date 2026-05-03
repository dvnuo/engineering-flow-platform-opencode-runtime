import json
from datetime import UTC, datetime

import pytest
from aiohttp.test_utils import TestClient, TestServer
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from test_t06_helpers import FakeOpenCodeClient

@pytest.mark.asyncio
async def test_usage_api(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path/'state'))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app)); await client.start_server()
    r = await client.get('/api/usage'); assert r.status==200
    p = await r.json(); assert p['global']['total_requests']==0
    await client.post('/api/chat', json={'message':'hello'})
    p2 = await (await client.get('/api/usage?days=30')).json()
    assert p2['global']['total_requests'] >= 1
    assert p2['global']['total_messages'] >= 2
    assert (tmp_path/'state'/'usage.jsonl').exists()
    assert p2['global']['total_input_tokens'] >= 10
    p3 = await (await client.get('/api/usage?days=bad')).json(); assert p3['period_days']==30
    await client.close()


@pytest.mark.asyncio
async def test_usage_api_handles_malformed_historical_usage_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    usage_file = state_dir / "usage.jsonl"
    usage_file.write_text(
        json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "type": "chat",
                "session_id": "s",
                "request_id": "r",
                "model": "m",
                "provider": "p",
                "requests": "bad",
                "messages": "bad",
                "input_tokens": "bad",
                "output_tokens": None,
                "cost": "nan",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()

    resp = await client.get("/api/usage?days=30")
    assert resp.status == 200
    text = await resp.text()
    assert "NaN" not in text
    assert "Infinity" not in text

    body = json.loads(text)
    assert body["global"]["total_requests"] == 0
    assert body["global"]["total_messages"] == 0
    assert body["global"]["total_input_tokens"] == 0
    assert body["global"]["total_output_tokens"] == 0
    assert body["global"]["total_cost"] == 0.0
    assert any(row["model"] == "m" for row in body["by_model"])
    assert any(row["provider"] == "p" for row in body["by_provider"])

    await client.close()
