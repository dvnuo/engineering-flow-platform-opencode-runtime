from pathlib import Path
import yaml

def _workflow():
    return yaml.safe_load((Path(__file__).resolve().parents[1]/'.github/workflows/ci.yml').read_text())

def test_unit_matrix_contract():
    jobs=_workflow()['jobs']['unit-tests']
    assert jobs['strategy']['fail-fast'] is False
    versions=set(jobs['strategy']['matrix']['python-version'])
    assert {'3.11','3.12'} <= versions
    steps=jobs['steps']
    setup_python=[step for step in steps if step.get('uses') == 'actions/setup-python@v5']
    assert setup_python
    assert setup_python[0]['with']['python-version'] == '${{ matrix.python-version }}'

def test_docker_smoke_contracts():
    wf=(Path(__file__).resolve().parents[1]/'.github/workflows/ci.yml').read_text()
    assert 'RUN_RUNTIME_CONTRACT_TESTS' in wf
    assert 'RUNTIME_CONTRACT_ENABLE_CHAT' not in wf
    assert 'RUNTIME_CONTRACT_ENABLE_TASKS' not in wf
