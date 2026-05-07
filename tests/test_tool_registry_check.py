import json

import pytest

from efp_opencode_adapter import tool_registry_check


@pytest.mark.asyncio
async def test_tool_registry_check_success(monkeypatch, capsys):
    async def fake_list(self, timeout_seconds=30):
        return ["efp_smoke_tool"]

    monkeypatch.setattr("efp_opencode_adapter.opencode_client.OpenCodeClient.list_tool_ids", fake_list)
    code = await tool_registry_check._run(30, ["efp_smoke_tool"], None)
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"


@pytest.mark.asyncio
async def test_tool_registry_check_missing_expected_tool(monkeypatch, capsys):
    async def fake_list(self, timeout_seconds=30):
        return []

    monkeypatch.setattr("efp_opencode_adapter.opencode_client.OpenCodeClient.list_tool_ids", fake_list)
    code = await tool_registry_check._run(30, ["efp_smoke_tool"], None)
    assert code == 1
    assert "OpenCode ToolRegistry readiness check failed" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_tool_registry_check_failure_message(monkeypatch, capsys):
    async def fake_list(self, timeout_seconds=30):
        raise RuntimeError("boom")

    monkeypatch.setattr("efp_opencode_adapter.opencode_client.OpenCodeClient.list_tool_ids", fake_list)
    code = await tool_registry_check._run(30, [], None)
    assert code == 1
    assert "OpenCode ToolRegistry readiness check failed" in capsys.readouterr().err
