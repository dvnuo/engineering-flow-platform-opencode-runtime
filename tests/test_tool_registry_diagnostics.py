import json

import pytest

from efp_opencode_adapter import tool_registry_diagnostics


@pytest.mark.asyncio
async def test_diagnostics_json_shape_and_no_password_leak(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "SECRET")

    async def fake_probe(settings, path, timeout):
        return {"ok": path == "/global/health", "status": 200, "payload_summary": "ok"}

    monkeypatch.setattr(tool_registry_diagnostics, "_probe_http", fake_probe)
    monkeypatch.setattr(tool_registry_diagnostics, "_opencode_binary_version", lambda: "opencode version 1.14.39")

    code = await tool_registry_diagnostics._run("http://127.0.0.1:4096", tmp_path, 3)
    assert code == 0
    out = capsys.readouterr().out
    assert "SECRET" not in out
    payload = json.loads(out)
    assert "versions" in payload
    assert "paths" in payload
    assert "workspace" in payload
    assert "http" in payload


@pytest.mark.asyncio
async def test_diagnostics_timeout_error_reported(monkeypatch, tmp_path, capsys):
    async def fake_probe(settings, path, timeout):
        if path == "/experimental/tool/ids":
            return {"ok": False, "error_type": "TimeoutError", "error_repr": "TimeoutError()"}
        return {"ok": True, "status": 200, "payload_summary": "ok"}

    monkeypatch.setattr(tool_registry_diagnostics, "_probe_http", fake_probe)
    code = await tool_registry_diagnostics._run("http://127.0.0.1:4096", tmp_path, 3)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["http"]["tool_ids"]["error_type"] == "TimeoutError"


@pytest.mark.asyncio
async def test_diagnostics_missing_files_still_returns_json(monkeypatch, tmp_path, capsys):
    async def fake_probe(settings, path, timeout):
        return {"ok": False, "error_type": "ServerDisconnectedError", "error_repr": "ServerDisconnectedError()"}

    monkeypatch.setattr(tool_registry_diagnostics, "_probe_http", fake_probe)
    code = await tool_registry_diagnostics._run("http://127.0.0.1:4096", tmp_path, 3)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["versions"]["workspace_plugin_version"] is None
