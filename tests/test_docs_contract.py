from pathlib import Path

def _t(p):
    return Path(p).read_text(encoding='utf-8')

def test_docs_contract_keywords():
    r=_t('docs/RUNTIME_CONTRACT.md')
    assert ':8000' in r and ':4096' in r and 'EFP_ADAPTER_STATE_DIR' in r and 'OPENCODE_DATA_DIR' in r and '/api/capabilities' in r and '/api/tasks/execute' in r
    o=_t('docs/OBSERVABILITY.md')
    for k in ['trace_id','agent_id','request_id','task_id','tool_source','data.trace_context','EventBus filter keys']:
        assert k in o
    t=_t('docs/TESTING.md')
    for k in ['scripts/ci_unit.sh','scripts/smoke.sh','RUN_RUNTIME_CONTRACT_TESTS','RUNTIME_CONTRACT_ENABLE_CHAT','RUNTIME_CONTRACT_ENABLE_TASKS']:
        assert k in t
    readme=_t('README.md')
    for k in ['docs/RUNTIME_CONTRACT.md','docs/OBSERVABILITY.md','docs/TESTING.md']:
        assert k in readme
    assert 'No Portal startup required' in _t('integration/README.md')
