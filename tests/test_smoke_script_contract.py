from pathlib import Path


def _script() -> str:
    return (Path(__file__).resolve().parents[1] / 'scripts' / 'smoke.sh').read_text(encoding='utf-8')


def test_smoke_script_asserts_skill_tool_mapping_contract():
    script = _script()
    assert 'legacy_name' in script
    assert 'smoke_tool' in script
    assert 'efp_smoke_tool' in script
    assert 'smoke_tool -> efp_smoke_tool' in script
    assert 'opencode_tools' in script
    assert 'tool_mappings' in script


def test_smoke_script_can_run_runtime_contract_tests():
    script = _script()
    assert 'RUN_RUNTIME_CONTRACT_TESTS' in script
    assert 'RUNTIME_CONTRACT_BASE_URL' in script
    assert 'RUNTIME_BASE_URL=' in script
    assert 'python -m pytest -q runtime_contract_tests' in script


def test_smoke_script_does_not_enable_live_llm_contracts_by_default():
    script = _script()
    assert 'RUNTIME_CONTRACT_ENABLE_CHAT=1' not in script
    assert 'RUNTIME_CONTRACT_ENABLE_TASKS=1' not in script


def test_smoke_script_runs_contract_tests_after_restart():
    script = _script()
    assert script.count('run_runtime_contract_tests') >= 3
    assert 'docker restart' in script


def test_smoke_script_dumps_docker_logs_on_failure():
    script = _script()
    assert 'docker logs' in script
    assert 'dump_logs_on_failure' in script
