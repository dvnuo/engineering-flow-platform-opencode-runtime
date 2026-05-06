import json
from efp_opencode_adapter.trace_context import build_trace_context, add_trace_context, profile_version_from_metadata

class S:
    portal_agent_id='agent-1'

def test_trace_context_basics_and_fallbacks():
    tc=build_trace_context(S())
    assert all(k in tc for k in ["engine","runtime_type","agent_id","trace_id","request_id","session_id","task_id","opencode_session_id"])
    assert tc['agent_id']=='agent-1'
    assert tc['trace_id']==''
    assert build_trace_context(S(),request_id='r',task_id='t')['trace_id']=='r'
    assert build_trace_context(S(),task_id='t')['trace_id']=='t'
    assert build_trace_context(S(),session_id='s')['trace_id']=='s'

def test_trace_sanitization_and_add():
    tc=build_trace_context(S(),request_id='token-abc',session_id='s')
    dumped=json.dumps(tc)
    assert 'token-abc' not in dumped
    event={'type':'x','request_id':'keep','data':{}}
    out=add_trace_context(event,tc)
    assert out['request_id']=='keep'
    assert out['trace_context']['session_id']==out['data']['trace_context']['session_id']

def test_profile_priority():
    pv,rid=profile_version_from_metadata({'runtime_profile_revision':7,'runtime_profile_id':'rp-1'},{'revision':'1','id':'x'})
    assert pv=='7' and rid=='rp-1'
