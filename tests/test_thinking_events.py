import json

from efp_opencode_adapter.thinking_events import assistant_delta_event, chat_failed_event, chat_started_event, llm_thinking_event, permission_request_event, safe_preview

def test_events_shapes():
    e = chat_started_event(session_id='s', request_id='r')
    assert e['type'] == 'execution.started' and e['event_type'] == 'execution.started'
    t = llm_thinking_event(session_id='s', request_id='r')
    assert t['session_id'] == 's' and t['request_id'] == 'r'
    d = assistant_delta_event(session_id='s', request_id='r', text='x'*1000)
    assert len(d['data']['delta']) < 600

def test_safe_preview_redact():
    v = safe_preview({'password':'a','nested':[{'token':'b'}],'Authorization':'Bearer abc','line':'OPENAI_API_KEY=abc'})
    assert v['password'] == '***REDACTED***'
    assert v['nested'][0]['token'] == '***REDACTED***'
    assert v['Authorization'] == '***REDACTED***'
    assert 'abc' not in v['line']

def test_permission_event():
    e = permission_request_event(session_id='s', request_id='r', permission_id='p1', tool='bash', input_preview='rm -rf', risk_level='medium')
    assert e['type'] == 'permission_request'
    assert e['permission_id'] == 'p1'


def test_safe_preview_redacts_authorization_header_line():
    v = safe_preview("Authorization: Bearer abc123")
    assert "abc123" not in v
    assert "***REDACTED***" in v


def test_safe_preview_redacts_github_and_openai_token_prefixes():
    assert "gho_SECRET" not in safe_preview("hello gho_SECRET")
    assert "ghu_SECRET" not in safe_preview("hello ghu_SECRET")
    assert "ghp_SECRET" not in safe_preview("hello ghp_SECRET")
    assert "github_pat_SECRET" not in safe_preview("hello github_pat_SECRET")
    assert "sk-SECRET" not in safe_preview("hello sk-SECRET")
    assert "hello world" in safe_preview("hello world")


def test_chat_failed_event_redacts_free_form_tokens():
    event = chat_failed_event(error="bad token gho_SECRET", session_id="s", request_id="r")
    assert "gho_SECRET" not in json.dumps(event)
