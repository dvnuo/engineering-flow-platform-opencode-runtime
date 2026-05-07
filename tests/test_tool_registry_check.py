import json

import pytest

from efp_opencode_adapter import tool_registry_check


@pytest.mark.asyncio
async def test_tool_registry_check_success(monkeypatch, capsys):
    async def fake_list(self, timeout_seconds=30):
        return ["efp_smoke_tool"]

    monkeypatch.setattr("efp_opencode_adapter.opencode_client.OpenCodeClient.list_tool_ids", fake_list)
    code = await tool_registry_check._run(30, 5, ["efp_smoke_tool"], None)
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"


@pytest.mark.asyncio
async def test_tool_registry_check_missing_expected_tool(monkeypatch, capsys):
    async def fake_list(self, timeout_seconds=30):
        return []

    monkeypatch.setattr("efp_opencode_adapter.opencode_client.OpenCodeClient.list_tool_ids", fake_list)
    code = await tool_registry_check._run(2, 1, ["efp_smoke_tool"], None)
    assert code == 1
    assert "OpenCode ToolRegistry readiness check failed" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_tool_registry_check_failure_message(monkeypatch, capsys):
    async def fake_list(self, timeout_seconds=30):
        raise RuntimeError("boom")

    monkeypatch.setattr("efp_opencode_adapter.opencode_client.OpenCodeClient.list_tool_ids", fake_list)
    code = await tool_registry_check._run(2, 1, [], None)
    assert code == 1
    assert "OpenCode ToolRegistry readiness check failed" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_tool_registry_check_retries_then_succeeds(monkeypatch, capsys):
    calls = {"count": 0}

    async def fake_list(self, timeout_seconds=30):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary")
        return ["efp_smoke_tool"]

    monkeypatch.setattr("efp_opencode_adapter.opencode_client.OpenCodeClient.list_tool_ids", fake_list)
    code = await tool_registry_check._run(3, 1, ["efp_smoke_tool"], None)
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["attempts"] >= 2


@pytest.mark.asyncio
async def test_tool_registry_check_failure_includes_attempts_and_last_error(monkeypatch, capsys):
    async def fake_list(self, timeout_seconds=30):
        raise RuntimeError("boom")

    monkeypatch.setattr("efp_opencode_adapter.opencode_client.OpenCodeClient.list_tool_ids", fake_list)
    code = await tool_registry_check._run(2, 1, [], None)
    assert code == 1
    err = capsys.readouterr().err
    assert "OpenCode ToolRegistry readiness check failed" in err
    assert "attempts" in err
    assert "boom" in err
