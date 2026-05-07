import json

import pytest

from efp_opencode_adapter import portal_runtime_context_bootstrap as mod


@pytest.mark.asyncio
async def test_env_missing_skips(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PORTAL_INTERNAL_BASE_URL", raising=False)
    monkeypatch.delenv("PORTAL_AGENT_ID", raising=False)
    code = await mod._run(tmp_path)
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "skipped"
