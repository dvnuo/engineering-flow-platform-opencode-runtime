import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter.event_bridge import OpenCodeEventBridge, normalize_opencode_event
from efp_opencode_adapter.event_bus import EventBus
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.session_store import SessionRecord, SessionStore
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.state import ensure_state_dirs
from efp_opencode_adapter.task_store import TaskStore


class FakeClient:
    async def health(self): return {"healthy": True}
    async def event_stream(self, **kwargs):
        if False:
            yield {}


@pytest.mark.asyncio
async def test_normalizes_permission_event_and_maps_portal_session(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    settings = Settings.from_env()
    settings.adapter_state_dir.mkdir(parents=True, exist_ok=True)
    paths = ensure_state_dirs(settings)
    session_store = SessionStore(paths.sessions_dir)
    session_store.upsert(SessionRecord("portal-1", "oc-1", "t", None, None, "a", "a", "", 0))
    task_store = TaskStore(paths.tasks_dir)
    bus = EventBus()
    bridge = OpenCodeEventBridge(settings, FakeClient(), bus, session_store, task_store)
    q = bus.subscribe({"session_id": "portal-1"})
    event = await bridge.publish_raw_event({"payload": {"type": "permission.asked", "properties": {"sessionID": "oc-1", "requestID": "perm-1"}}})
    got = await asyncio.wait_for(q.queue.get(), timeout=1)
    assert got["type"] == "permission_request"
    assert event and event["permission_id"] == "perm-1"


@pytest.mark.asyncio
async def test_normalizes_tool_events(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    settings = Settings.from_env()
    settings.adapter_state_dir.mkdir(parents=True, exist_ok=True)
    bridge = OpenCodeEventBridge(settings, FakeClient(), EventBus(), SessionStore(ensure_state_dirs(settings).sessions_dir), TaskStore(ensure_state_dirs(settings).tasks_dir))
    s = await bridge.publish_raw_event({"type": "tool.start"})
    c = await bridge.publish_raw_event({"type": "tool.complete"})
    assert s["type"] == "tool.started"
    assert c["type"] == "tool.completed"


def test_create_app_does_not_auto_start_bridge_for_injected_fake_client(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    app = create_app(Settings.from_env(), opencode_client=FakeClient())
    assert "event_bridge" not in app


@pytest.mark.asyncio
async def test_create_app_can_force_start_bridge_for_injected_fake_client(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    app = create_app(Settings.from_env(), opencode_client=FakeClient(), start_event_bridge=True)
    client = TestClient(TestServer(app))
    await client.start_server()
    await client.close()


@pytest.mark.asyncio
async def test_event_bridge_redacts_secret_strings_and_top_level_tool_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    settings = Settings.from_env()
    settings.adapter_state_dir.mkdir(parents=True, exist_ok=True)
    bridge = OpenCodeEventBridge(settings, FakeClient(), EventBus(), SessionStore(ensure_state_dirs(settings).sessions_dir), TaskStore(ensure_state_dirs(settings).tasks_dir))
    event = await bridge.publish_raw_event({"type": "tool.start", "sessionID": "oc-1", "tool": "efp_secret_tool", "input": "use token SECRET-KEY-SHOULD-NOT-LEAK here"})
    encoded = json.dumps(event).lower()
    assert "secret-key-should-not-leak" not in encoded
    assert "token" not in encoded
    assert event["type"] == "tool.started"
    assert event["tool"]
    assert "input_preview" in event


@pytest.mark.asyncio
async def test_permission_updated_with_approved_status_is_resolved(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    settings = Settings.from_env()
    bridge = OpenCodeEventBridge(settings, FakeClient(), EventBus(), SessionStore(ensure_state_dirs(settings).sessions_dir), TaskStore(ensure_state_dirs(settings).tasks_dir))
    event = await bridge.publish_raw_event({"payload": {"type": "permission.updated", "properties": {"sessionID": "oc-1", "permissionID": "perm-1", "status": "approved", "tool": "bash"}}})
    assert event["type"] == "permission_resolved"
    assert event["permission_id"] == "perm-1"
    assert event["tool"] == "bash"
    assert "decision" in event


@pytest.mark.asyncio
async def test_message_part_updated_extracts_delta_text(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    settings = Settings.from_env()
    bridge = OpenCodeEventBridge(settings, FakeClient(), EventBus(), SessionStore(ensure_state_dirs(settings).sessions_dir), TaskStore(ensure_state_dirs(settings).tasks_dir))
    event = await bridge.publish_raw_event({"type": "message.part.updated", "sessionID": "oc-1", "part": {"type": "text", "text": "hello delta"}})
    assert event["type"] == "message.delta"
    assert "hello delta" in event["data"]["delta"]


@pytest.mark.asyncio
async def test_event_bridge_redacts_top_level_request_id(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    settings = Settings.from_env()
    bridge = OpenCodeEventBridge(settings, FakeClient(), EventBus(), SessionStore(ensure_state_dirs(settings).sessions_dir), TaskStore(ensure_state_dirs(settings).tasks_dir))
    event = await bridge.publish_raw_event({"type": "permission.asked", "sessionID": "oc-1", "requestID": "token SECRET-KEY-SHOULD-NOT-LEAK", "tool": "bash"})
    encoded = json.dumps(event).lower()
    assert "secret-key-should-not-leak" not in encoded
    assert "token" not in encoded
    assert event["type"] == "permission_request"
    assert event["request_id"] == "[redacted]"
    assert event["permission_id"] == "[redacted]"


@pytest.mark.asyncio
async def test_event_bridge_uses_raw_session_for_mapping_but_sanitizes_output(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    settings = Settings.from_env()
    paths = ensure_state_dirs(settings)
    session_store = SessionStore(paths.sessions_dir)
    session_store.upsert(SessionRecord("portal-1", "oc-secret-token", "t", None, None, "a", "a", "", 0))
    bridge = OpenCodeEventBridge(settings, FakeClient(), EventBus(), session_store, TaskStore(paths.tasks_dir))
    event = await bridge.publish_raw_event({"type": "tool.start", "sessionID": "oc-secret-token", "tool": "bash", "input": "echo ok"})
    assert event["session_id"] == "portal-1"
    assert event["opencode_session_id"] == "[redacted]"
    assert "oc-secret-token" not in json.dumps(event).lower()


@pytest.mark.asyncio
async def test_permission_updated_response_allow_is_resolved(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    settings = Settings.from_env()
    bridge = OpenCodeEventBridge(settings, FakeClient(), EventBus(), SessionStore(ensure_state_dirs(settings).sessions_dir), TaskStore(ensure_state_dirs(settings).tasks_dir))
    event = await bridge.publish_raw_event({"payload": {"type": "permission.updated", "properties": {"id": "perm-1", "response": "allow", "tool": "bash"}}})
    assert event["type"] == "permission_resolved"
    assert event["permission_id"] == "perm-1"
    assert event["tool"] == "bash"
    assert event.get("decision") == "allow"


@pytest.mark.asyncio
async def test_permission_updated_response_deny_is_resolved(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    settings = Settings.from_env()
    bridge = OpenCodeEventBridge(settings, FakeClient(), EventBus(), SessionStore(ensure_state_dirs(settings).sessions_dir), TaskStore(ensure_state_dirs(settings).tasks_dir))
    event = await bridge.publish_raw_event({"payload": {"type": "permission.updated", "properties": {"id": "perm-2", "response": "deny", "tool": "bash"}}})
    assert event["type"] == "permission_resolved"
    assert event["permission_id"] == "perm-2"
    assert event.get("decision") == "deny"


@pytest.mark.asyncio
async def test_permission_updated_without_status_or_response_remains_request(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    settings = Settings.from_env()
    bridge = OpenCodeEventBridge(settings, FakeClient(), EventBus(), SessionStore(ensure_state_dirs(settings).sessions_dir), TaskStore(ensure_state_dirs(settings).tasks_dir))
    event = await bridge.publish_raw_event({"payload": {"type": "permission.updated", "properties": {"id": "perm-3", "tool": "bash"}}})
    assert event["type"] == "permission_request"
    assert event["permission_id"] == "perm-3"


@pytest.mark.asyncio
async def test_tool_event_enriched_with_mutation_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    settings = Settings.from_env()
    (settings.adapter_state_dir).mkdir(parents=True, exist_ok=True)
    (settings.adapter_state_dir / "tools-index.json").write_text(json.dumps({"tools":[{"name":"efp_github_add_comment","opencode_name":"efp_github_add_comment","legacy_name":"github_add_comment","capability_id":"efp.tool.github.add_comment","policy_tags":["github","mutation"],"requires_identity_binding":True,"risk_level":"high","mutation":True,"source_ref":"tools/github/github_add_comment.yaml"}]}), encoding="utf-8")
    bridge = OpenCodeEventBridge(settings, FakeClient(), EventBus(), SessionStore(ensure_state_dirs(settings).sessions_dir), TaskStore(ensure_state_dirs(settings).tasks_dir))
    event = await bridge.publish_raw_event({"type":"tool.start","sessionID":"oc-1","tool":"efp_github_add_comment"})
    assert event["type"] == "tool.started"
    assert event["capability_id"] == "efp.tool.github.add_comment"
    assert event["mutation"] is True and event["audit_event"] is True and event["requires_identity_binding"] is True
    assert "mutation" in event["policy_tags"] and event["data"]["mutation"] is True


@pytest.mark.asyncio
async def test_tool_event_enriched_by_legacy_name(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    settings = Settings.from_env()
    (settings.adapter_state_dir).mkdir(parents=True, exist_ok=True)
    (settings.adapter_state_dir / "tools-index.json").write_text(json.dumps({"tools":[{"opencode_name":"efp_github_add_comment","legacy_name":"github_add_comment","capability_id":"efp.tool.github.add_comment","policy_tags":["mutation"]}]}), encoding="utf-8")
    bridge = OpenCodeEventBridge(settings, FakeClient(), EventBus(), SessionStore(ensure_state_dirs(settings).sessions_dir), TaskStore(ensure_state_dirs(settings).tasks_dir))
    event = await bridge.publish_raw_event({"type":"tool.start","sessionID":"oc-1","tool":"github_add_comment"})
    assert event["capability_id"] == "efp.tool.github.add_comment"


@pytest.mark.asyncio
async def test_tool_source_trace_context_tools_repo_builtin_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("PORTAL_AGENT_ID", "agent-bridge-1")
    settings = Settings.from_env()
    settings.adapter_state_dir.mkdir(parents=True, exist_ok=True)
    (settings.adapter_state_dir / "tools-index.json").write_text(json.dumps({"tools": [{"name": "efp_context_echo", "opencode_name": "efp_context_echo", "legacy_name": "context_echo", "source_ref": "tools_repo", "risk_level": "low", "policy_tags": ["read_only"]}]}), encoding="utf-8")
    session_store = SessionStore(ensure_state_dirs(settings).sessions_dir)
    task_store = TaskStore(ensure_state_dirs(settings).tasks_dir)
    bridge = OpenCodeEventBridge(settings, FakeClient(), EventBus(), session_store, task_store)
    e1 = normalize_opencode_event({"type": "tool.start", "sessionID": "oc-1", "tool": "efp_context_echo", "input": "hello"}, session_store=session_store, task_store=task_store, settings=settings, tool_metadata={"efp_context_echo": {"source_ref": "tools_repo"}})
    assert e1["tool_source"] == "tools_repo" and e1["tool_name"] == "efp_context_echo"
    assert e1["trace_context"]["tool_source"] == "tools_repo"
    assert e1["data"]["trace_context"]["tool_name"] == "efp_context_echo"
    assert e1["trace_context"]["agent_id"] == "agent-bridge-1"


@pytest.mark.asyncio
async def test_session_status_retry_normalized_provider_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    settings = Settings.from_env()
    bridge = OpenCodeEventBridge(settings, FakeClient(), EventBus(), SessionStore(ensure_state_dirs(settings).sessions_dir), TaskStore(ensure_state_dirs(settings).tasks_dir))
    event = await bridge.publish_raw_event({"payload": {"type": "session.status", "properties": {"status": {"type": "retry", "attempt": 14, "message": "Cannot connect to API"}}}})
    assert event["type"] == "provider.retry"
    assert event["state"] == "retrying"
    assert event["data"]["attempt"] == 14
    assert "Cannot connect to API" in event["data"]["message"]



@pytest.mark.asyncio
async def test_read_only_tool_event_not_audit_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    settings = Settings.from_env()
    (settings.adapter_state_dir).mkdir(parents=True, exist_ok=True)
    (settings.adapter_state_dir / "tools-index.json").write_text(json.dumps({"tools":[{"opencode_name":"efp_github_get_pr","legacy_name":"github_get_pr","capability_id":"efp.tool.github.get_pr","policy_tags":["github","read_only"],"risk_level":"low","mutation":False,"requires_identity_binding":False}]}), encoding="utf-8")
    bridge = OpenCodeEventBridge(settings, FakeClient(), EventBus(), SessionStore(ensure_state_dirs(settings).sessions_dir), TaskStore(ensure_state_dirs(settings).tasks_dir))
    event = await bridge.publish_raw_event({"type":"tool.start","sessionID":"oc-1","tool":"efp_github_get_pr"})
    assert event["mutation"] is False and event["audit_event"] is False

@pytest.mark.asyncio
async def test_unknown_tool_event_has_stable_false_audit_fields(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state')); monkeypatch.setenv('EFP_WORKSPACE_DIR', str(tmp_path / 'workspace'))
    settings = Settings.from_env(); bridge = OpenCodeEventBridge(settings, FakeClient(), EventBus(), SessionStore(ensure_state_dirs(settings).sessions_dir), TaskStore(ensure_state_dirs(settings).tasks_dir))
    event = await bridge.publish_raw_event({'type':'tool.start','tool':'unknown_tool'})
    assert event['mutation'] is False and event['audit_event'] is False and event['policy_tags'] == []
    assert event['data']['mutation'] is False and event['data']['audit_event'] is False

@pytest.mark.asyncio
async def test_refresh_tool_metadata_picks_up_updated_tools_index(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state')); monkeypatch.setenv('EFP_WORKSPACE_DIR', str(tmp_path / 'workspace'))
    settings = Settings.from_env(); settings.adapter_state_dir.mkdir(parents=True, exist_ok=True)
    (settings.adapter_state_dir / 'tools-index.json').write_text(json.dumps({'tools':[]}), encoding='utf-8')
    bridge = OpenCodeEventBridge(settings, FakeClient(), EventBus(), SessionStore(ensure_state_dirs(settings).sessions_dir), TaskStore(ensure_state_dirs(settings).tasks_dir))
    event1 = await bridge.publish_raw_event({'type':'tool.start','tool':'efp_refresh'})
    assert event1['audit_event'] is False
    (settings.adapter_state_dir / 'tools-index.json').write_text(json.dumps({'tools':[{'capability_id':'tool.refresh','opencode_name':'efp_refresh','mutation':True,'risk_level':'high'}]}), encoding='utf-8')
    bridge.refresh_tool_metadata(); event2 = await bridge.publish_raw_event({'type':'tool.start','tool':'efp_refresh'})
    assert event2['mutation'] is True and event2['audit_event'] is True

@pytest.mark.asyncio
async def test_tool_metadata_prefers_enabled_descriptor(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state')); monkeypatch.setenv('EFP_WORKSPACE_DIR', str(tmp_path / 'workspace'))
    settings = Settings.from_env(); settings.adapter_state_dir.mkdir(parents=True, exist_ok=True)
    (settings.adapter_state_dir / 'tools-index.json').write_text(json.dumps({'tools':[{'capability_id':'tool.same.disabled','opencode_name':'efp_same','enabled':False,'risk_level':'high','mutation':True},{'capability_id':'tool.same.enabled','opencode_name':'efp_same','enabled':True,'risk_level':'low','policy_tags':['read_only'],'mutation':False}]}), encoding='utf-8')
    bridge = OpenCodeEventBridge(settings, FakeClient(), EventBus(), SessionStore(ensure_state_dirs(settings).sessions_dir), TaskStore(ensure_state_dirs(settings).tasks_dir))
    event = await bridge.publish_raw_event({'type':'tool.start','tool':'efp_same'})
    assert event['risk_level'] == 'low' and event['mutation'] is False and event['audit_event'] is False

@pytest.mark.asyncio
async def test_policy_tags_dict_does_not_leak_secret(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state')); monkeypatch.setenv('EFP_WORKSPACE_DIR', str(tmp_path / 'workspace'))
    settings = Settings.from_env(); settings.adapter_state_dir.mkdir(parents=True, exist_ok=True)
    (settings.adapter_state_dir / 'tools-index.json').write_text(json.dumps({'tools':[{'opencode_name':'efp_bad_tags','capability_id':'tool.bad_tags','policy_tags':{'token':'SECRET-SHOULD-NOT-LEAK'},'risk_level':'low','mutation':False}]}), encoding='utf-8')
    bridge = OpenCodeEventBridge(settings, FakeClient(), EventBus(), SessionStore(ensure_state_dirs(settings).sessions_dir), TaskStore(ensure_state_dirs(settings).tasks_dir))
    event = await bridge.publish_raw_event({'type':'tool.start','tool':'efp_bad_tags'})
    assert event['policy_tags'] == []
    assert 'SECRET-SHOULD-NOT-LEAK' not in json.dumps(event)
    assert event['audit_event'] is False

@pytest.mark.asyncio
async def test_policy_tags_dict_mutation_key_is_ignored_and_does_not_leak_secret(tmp_path, monkeypatch):
    monkeypatch.setenv('EFP_ADAPTER_STATE_DIR', str(tmp_path / 'state')); monkeypatch.setenv('EFP_WORKSPACE_DIR', str(tmp_path / 'workspace'))
    settings = Settings.from_env(); settings.adapter_state_dir.mkdir(parents=True, exist_ok=True)
    (settings.adapter_state_dir / 'tools-index.json').write_text(json.dumps({'tools':[{'opencode_name':'efp_bad_tags2','capability_id':'tool.bad_tags2','policy_tags':{'mutation':'SECRET-SHOULD-NOT-LEAK'},'mutation':False,'risk_level':'low','requires_identity_binding':False}]}), encoding='utf-8')
    bridge = OpenCodeEventBridge(settings, FakeClient(), EventBus(), SessionStore(ensure_state_dirs(settings).sessions_dir), TaskStore(ensure_state_dirs(settings).tasks_dir))
    event = await bridge.publish_raw_event({'type':'tool.start','tool':'efp_bad_tags2'})
    assert event['policy_tags'] == []
    assert event['mutation'] is False
    assert event['audit_event'] is False
    assert 'SECRET-SHOULD-NOT-LEAK' not in json.dumps(event)


def test_event_bridge_part_type_classification(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    settings = Settings.from_env()
    session_store = SessionStore(ensure_state_dirs(settings).sessions_dir)
    task_store = TaskStore(ensure_state_dirs(settings).tasks_dir)

    step = normalize_opencode_event({"payload": {"type": "sync", "syncEvent": {"type": "message.part.updated.1", "data": {"part": {"type": "step-finish"}}}}}, session_store=session_store, task_store=task_store, settings=settings)
    assert step["type"] != "assistant_delta"

    reason = normalize_opencode_event({"type": "message.part.updated", "part": {"type": "reasoning", "text": "hidden reasoning"}}, session_store=session_store, task_store=task_store, settings=settings)
    assert reason["type"] == "llm_thinking"
    assert "delta" not in reason["data"]

    text = normalize_opencode_event({"type": "message.part.updated", "part": {"type": "text", "text": "Hi"}}, session_store=session_store, task_store=task_store, settings=settings)
    assert text["type"] in {"message.delta", "assistant_delta"}
    assert text["data"].get("delta") == "Hi"


@pytest.mark.asyncio
async def test_sync_step_finish_not_delta_or_execution_completed(tmp_path, monkeypatch):
    monkeypatch.setenv("EFP_ADAPTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EFP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    settings = Settings.from_env()
    bridge = OpenCodeEventBridge(settings, FakeClient(), EventBus(), SessionStore(ensure_state_dirs(settings).sessions_dir), TaskStore(ensure_state_dirs(settings).tasks_dir))
    event = await bridge.publish_raw_event({"payload": {"type": "sync", "syncEvent": {"type": "message.part.updated.1", "id": "evt-1", "data": {"sessionID": "ses-1", "part": {"type": "step-finish", "reason": "stop"}}}}})
    assert event["type"] not in {"assistant_delta", "message.delta", "execution.completed"}
    assert "delta" not in event["data"]
