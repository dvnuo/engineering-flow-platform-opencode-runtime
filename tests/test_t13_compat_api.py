from efp_opencode_adapter.app_keys import TASK_STORE_KEY
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.compat_api import _clean_repo_url
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.task_store import TaskRecord
from test_t06_helpers import FakeOpenCodeClient


@pytest.mark.asyncio
async def test_t13_skills_endpoint_returns_indexed_skills(tmp_path, monkeypatch):
    workspace, state, tools, skills = tmp_path / "workspace", tmp_path / "state", tmp_path / "tools", tmp_path / "skills"
    for p in (workspace / ".opencode", state, tools, skills):
        p.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state))
    monkeypatch.setenv("EFP_TOOLS_DIR", str(tools))
    monkeypatch.setenv("EFP_SKILLS_DIR", str(skills))
    monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    (state / "skills-index.json").write_text(json.dumps({"generated_at": "now", "skills": [{"efp_name": "collect_requirements_to_bundle", "opencode_name": "collect-requirements-to-bundle", "description": "Collect requirements", "tools": ["efp_context_read_ref"], "task_tools": [], "risk_level": "low", "source_path": "/app/skills/collect_requirements_to_bundle/skill.md", "target_path": "/workspace/.opencode/skills/collect-requirements-to-bundle/SKILL.md"}], "warnings": []}))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app)); await client.start_server()
    body = await (await client.get('/api/skills')).json()
    assert body['engine'] == 'opencode' and body['count'] == 1
    assert body['skills'][0]['name'] == 'collect-requirements-to-bundle'
    assert body['skills'][0]['efp_name'] == 'collect_requirements_to_bundle'
    await client.close()


@pytest.mark.asyncio
async def test_t13_queue_status_reports_task_counts(tmp_path, monkeypatch):
    workspace, state, tools, skills = tmp_path / "workspace", tmp_path / "state", tmp_path / "tools", tmp_path / "skills"
    for p in (workspace / ".opencode", state, tools, skills): p.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace)); monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state)); monkeypatch.setenv("EFP_TOOLS_DIR", str(tools)); monkeypatch.setenv("EFP_SKILLS_DIR", str(skills)); monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app)); await client.start_server()
    app[TASK_STORE_KEY].save(TaskRecord(task_id='t1', task_type='x', request_id='r1', status='running', portal_session_id='p', opencode_session_id='o', input_payload={}, metadata={}, output_payload=None, artifacts={}, runtime_events=[], error=None, created_at='now'))
    app[TASK_STORE_KEY].save(TaskRecord(task_id='t2', task_type='x', request_id='r2', status='blocked', portal_session_id='p', opencode_session_id='o', input_payload={}, metadata={}, output_payload=None, artifacts={}, runtime_events=[], error=None, created_at='now'))
    body = await (await client.get('/api/queue/status')).json()
    assert body['status'] == 'ok' and body['engine'] == 'opencode'
    assert body['queues']['default']['running'] == 1 and body['queues']['default']['blocked'] == 1 and body['queues']['default']['total'] == 2
    await client.close()


@pytest.mark.asyncio
async def test_t13_git_info_endpoints_are_stable_without_git_repo(tmp_path, monkeypatch):
    workspace, state, tools, skills = tmp_path / "workspace", tmp_path / "state", tmp_path / "tools", tmp_path / "skills"
    for p in (workspace / ".opencode", state, tools, skills): p.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace)); monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state)); monkeypatch.setenv("EFP_TOOLS_DIR", str(tools)); monkeypatch.setenv("EFP_SKILLS_DIR", str(skills)); monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient()))); await client.start_server()
    for path in ('/api/git-info', '/api/skill-git-info'):
        r = await client.get(path); body = await r.json(); assert r.status == 200 and body['engine'] == 'opencode' and 'commit_id' in body and 'repo_url' in body
    await client.close()


@pytest.mark.asyncio
async def test_t13_system_prompt_and_capabilities(tmp_path, monkeypatch):
    workspace, state, tools, skills = tmp_path / "workspace", tmp_path / "state", tmp_path / "tools", tmp_path / "skills"
    for p in (workspace / ".opencode", state, tools, skills): p.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(workspace)); monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(state)); monkeypatch.setenv("EFP_TOOLS_DIR", str(tools)); monkeypatch.setenv("EFP_SKILLS_DIR", str(skills)); monkeypatch.setenv("OPENCODE_CONFIG", str(workspace / ".opencode/opencode.json"))
    client = TestClient(TestServer(create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient()))); await client.start_server()
    cfg = await (await client.get('/api/agent/system-prompt/config')).json(); assert cfg['tools']['enabled'] is True
    assert (await client.put('/api/agent/system-prompt/config', json={'tools': {'enabled': False}})).status == 200
    cfg2 = await (await client.get('/api/agent/system-prompt/config')).json(); assert cfg2['tools']['enabled'] is False
    assert (await client.put('/api/agent/system-prompt/tools', json={'enabled': True, 'content': 'Tool guidance'})).status == 200
    sp = await (await client.get('/api/agent/system-prompt/tools')).json(); assert sp['enabled'] is True and 'Tool guidance' in sp['content']
    assert (await client.put('/api/agent/system-prompt/config', json={'bad': {'enabled': True}})).status == 400
    assert (await client.put('/api/agent/system-prompt/tools', json={'enabled': 'yes'})).status == 400
    cap = await (await client.get('/api/capabilities')).json(); assert cap['engine'] == 'opencode'
    await client.close()


def test_t13_git_repo_url_sanitization_strips_credentials_ports_and_query():
    cases = {
        "https://oauth2:SECRET@github.com:443/org/repo.git?token=abc": "https://github.com/org/repo.git",
        "https://token@github.com/org/repo.git": "https://github.com/org/repo.git",
        "http://user:pass@example.com:8080/x.git": "http://example.com/x.git",
        "ssh://git@github.com:2222/org/repo.git": "ssh://github.com/org/repo.git",
        "git@github.com:org/repo.git": "github.com:org/repo.git",
        "https://github.com/org/repo.git": "https://github.com/org/repo.git",
    }
    for raw, expected in cases.items():
        assert _clean_repo_url(raw) == expected

    encoded = " ".join(_clean_repo_url(x) or "" for x in cases)
    assert "SECRET" not in encoded
    assert "token=abc" not in encoded
    assert "user:pass" not in encoded
    assert ":443/" not in encoded
    assert ":8080/" not in encoded
    assert ":2222/" not in encoded
