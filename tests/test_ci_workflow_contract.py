from pathlib import Path


def _workflow() -> str:
    return (Path(__file__).resolve().parents[1] / '.github' / 'workflows' / 'ci.yml').read_text(encoding='utf-8')


def test_ci_docker_smoke_enables_runtime_contract_tests():
    workflow = _workflow()
    assert 'docker-smoke' in workflow
    assert 'RUN_RUNTIME_CONTRACT_TESTS' in workflow
    assert '"1"' in workflow or "'1'" in workflow


def test_ci_docker_smoke_installs_jq_for_smoke_script():
    workflow = _workflow()
    assert 'apt-get install' in workflow
    assert 'jq' in workflow


def test_ci_docker_smoke_runs_smoke_script():
    workflow = _workflow()
    assert 'bash scripts/smoke.sh' in workflow


def test_ci_docker_smoke_does_not_enable_live_llm_contracts():
    workflow = _workflow()
    assert 'RUNTIME_CONTRACT_ENABLE_CHAT' not in workflow
    assert 'RUNTIME_CONTRACT_ENABLE_TASKS' not in workflow
