from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RCT = ROOT / 'runtime_contract_tests'


def _read_all_contract_tests() -> str:
    return '\n'.join(p.read_text(encoding='utf-8') for p in RCT.glob('test_*.py'))


def test_runtime_contract_tests_cover_core_adapter_endpoints():
    text = _read_all_contract_tests()
    for endpoint in [
        '/health',
        '/actuator/health',
        '/api/capabilities',
        '/api/sessions',
        '/api/skills',
        '/api/queue/status',
        '/api/server-files',
    ]:
        assert endpoint in text


def test_runtime_contract_optional_live_llm_checks_are_env_gated():
    text = (RCT / 'test_optional_chat_task_contract.py').read_text(encoding='utf-8')
    assert 'RUNTIME_CONTRACT_ENABLE_CHAT' in text
    assert 'RUNTIME_CONTRACT_ENABLE_TASKS' in text
    assert 'pytest.skip' in text


def test_runtime_contract_base_url_required_but_no_portal_required():
    text = (RCT / 'conftest.py').read_text(encoding='utf-8')
    assert 'RUNTIME_BASE_URL' in text
    assert 'pytest.skip' in text
    assert 'Portal' not in text
