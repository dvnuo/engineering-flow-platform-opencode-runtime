import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.profile_store import sanitize_public_secrets
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings


class FakeClient:
    async def health(self):
        return {"healthy": True, "version": "1.14.29"}

    async def mcp(self):
        return {
            "success": True,
            "tools": [
                {"name": "github_status", "description": "GitHub status", "inputSchema": {"type": "object"}},
                {
                    "name": "safe_mcp_tool",
                    "description": "requires api_key in schema",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"query": {"type": "string", "description": "do not pass api_key token secret"}},
                        "required": ["query", "api_key"],
                    },
                },
            ],
        }


def test_sanitize_public_secrets_removes_keys_and_string_values():
    payload = {"description": "requires api_key token secret", "input_schema": {"properties": {"api_key": {"type": "string"}, "query": {"description": "api_key is not needed"}}, "required": ["api_key", "query"]}}
    clean = sanitize_public_secrets(payload)
    encoded = json.dumps(clean).lower()
    assert "api_key" not in encoded
    assert "token" not in encoded
    assert "secret" not in encoded
    assert "query" in encoded


@pytest.mark.asyncio
async def test_capabilities_catalog(tmp_path, monkeypatch):
    workspace, state, tools, skills = tmp_path / "workspace", tmp_path / "state", tmp_path / "tools", tmp_path / "skills"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("EFP_TOOLS_DIR", str(tools))
    monkeypatch.setenv("EFP_SKILLS_DIR", str(skills))
    monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    state.mkdir(parents=True)
    tools.mkdir(parents=True)
    (workspace / ".opencode").mkdir(parents=True)
    (state / "skills-index.json").write_text(json.dumps({"skills": [{"opencode_name": "my-skill", "description": "d", "efp_name": "e", "tools": [], "task_tools": []}]}))
    (tools / "manifest.yaml").write_text(
        "tools:\n"
        "  - capability_id: tool.read\n"
        "    opencode_name: efp_read\n"
        "    policy_tags: [read_only]\n"
        "    description: requires api_key token secret\n"
        "    input_schema:\n"
        "      type: object\n"
        "      properties:\n"
        "        query:\n"
        "          type: string\n"
        "          description: no api_key here\n"
        "        api_key: {type: string}\n"
        "      required: [api_key, query]\n"
        "  - capability_id: tool.native\n"
        "    opencode_name: native_only\n"
        "    runtime_compat: [native]\n"
        "  - capability_id: tool.open\n"
        "    opencode_name: opencode_tool\n"
        "    runtime_compat: [opencode]\n"
    )
    (workspace / ".opencode/opencode.json").write_text(json.dumps({"agent": {"efp-main": {"description": "Main"}}, "permission": {"skill": {"*": "deny", "my-skill": "allow"}}, "api_key": "SECRET"}))

    app = create_app(Settings.from_env(), opencode_client=FakeClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    payload = await (await client.get("/api/capabilities")).json()
    caps = payload["capabilities"]
    names = {c.get("name") for c in caps}
    assert {"read", "bash", "websearch", "my-skill", "efp_read", "efp-main", "github_status", "safe_mcp_tool", "opencode_tool"}.issubset(names)
    assert "native_only" not in names
    skill = next(c for c in caps if c.get("type") == "skill" and c.get("name") == "my-skill")
    assert skill["permission_state"] == "allowed"
    assert skill["callable"] is True
    skills_payload = await (await client.get("/api/skills")).json()
    s = next(i for i in skills_payload["skills"] if i["name"] == "my-skill")
    assert s["permission_state"] == "allowed"

    for c in caps:
        for key in ("capability_id", "type", "name", "enabled", "policy_tags", "source_ref"):
            assert key in c
    assert payload["engine"] == "opencode"
    assert payload["count"] == len(caps)
    assert payload["catalog_version"]
    assert payload["supports_snapshot_contract"] is True
    assert payload["runtime_contract_version"] == "efp-opencode-compat-v1"
    encoded = json.dumps(payload).lower()
    for marker in ("api_key", "token", "secret", "password", "authorization", "credential"):
        assert marker not in encoded
    assert "query" in encoded
    await client.close()


@pytest.mark.asyncio
async def test_denied_skill_state_exposed_in_capabilities_and_skills(tmp_path, monkeypatch):
    workspace, state, tools, skills = tmp_path / "workspace", tmp_path / "state", tmp_path / "tools", tmp_path / "skills"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("EFP_TOOLS_DIR", str(tools))
    monkeypatch.setenv("EFP_SKILLS_DIR", str(skills))
    monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    state.mkdir(parents=True)
    tools.mkdir(parents=True)
    (workspace / ".opencode").mkdir(parents=True)
    (state / "skills-index.json").write_text(json.dumps({"skills": [{"opencode_name": "denied-skill"}]}))
    (workspace / ".opencode/opencode.json").write_text(json.dumps({"permission": {"skill": {"*": "deny"}}}))
    app = create_app(Settings.from_env(), opencode_client=FakeClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    caps = await (await client.get("/api/capabilities")).json()
    denied_cap = next(c for c in caps["capabilities"] if c.get("type") == "skill" and c.get("name") == "denied-skill")
    assert denied_cap["permission_state"] == "denied"
    assert denied_cap["callable"] is False
    assert denied_cap["blocked_reason"]
    skills_payload = await (await client.get("/api/skills")).json()
    denied_skill = next(s for s in skills_payload["skills"] if s.get("name") == "denied-skill")
    assert denied_skill["permission_state"] == "denied"
    assert denied_skill["callable"] is False
    assert denied_skill["blocked_reason"]
    await client.close()

@pytest.mark.asyncio
async def test_unsupported_skill_is_not_callable_even_if_permission_allow(tmp_path, monkeypatch):
    workspace, state, tools, skills = tmp_path / "workspace", tmp_path / "state", tmp_path / "tools", tmp_path / "skills"
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace)); monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state)); monkeypatch.setenv("EFP_TOOLS_DIR", str(tools)); monkeypatch.setenv("EFP_SKILLS_DIR", str(skills)); monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    state.mkdir(parents=True); tools.mkdir(parents=True); (workspace / '.opencode').mkdir(parents=True)
    (state / 'skills-index.json').write_text(json.dumps({"skills":[{"efp_name":"native-only","opencode_name":"native-only","description":"Native only","tools":[],"task_tools":[],"risk_level":"low","source_path":"x","target_path":"y","opencode_supported":False,"opencode_compatibility":"unsupported","runtime_equivalence":False,"programmatic":False,"compatibility_warnings":["skill is marked unsupported for OpenCode runtime"]}]}), encoding='utf-8')
    (workspace / '.opencode/opencode.json').write_text(json.dumps({"permission":{"skill":{"native-only":"allow"}}}), encoding='utf-8')
    app = create_app(Settings.from_env(), opencode_client=FakeClient()); client = TestClient(TestServer(app)); await client.start_server()
    caps = await (await client.get('/api/capabilities')).json()
    skill = next(c for c in caps['capabilities'] if c.get('type') == 'skill' and c.get('name') == 'native-only')
    assert skill['callable'] is False
    assert 'not supported' in (skill.get('blocked_reason') or '')
    assert skill['metadata']['opencode_compatibility'] == 'unsupported'
    await client.close()
