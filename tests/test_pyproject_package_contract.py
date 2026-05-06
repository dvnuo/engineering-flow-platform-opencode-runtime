from pathlib import Path
import tomllib


def test_pyproject_setuptools_package_discovery_contract():
    payload = tomllib.loads(Path('pyproject.toml').read_text(encoding='utf-8'))
    find = payload['tool']['setuptools']['packages']['find']
    assert 'efp_opencode_adapter*' in find['include']
    for item in ('runtime_contract_tests*', 'integration*', 'tests*'):
        assert item in find['exclude']
    assert find['namespaces'] is False


def test_ci_and_docker_install_commands_still_present():
    workflow = Path('.github/workflows/ci.yml').read_text(encoding='utf-8')
    assert 'pip install -e ".[test]"' in workflow
    dockerfile = Path('Dockerfile').read_text(encoding='utf-8')
    assert 'pip install -e .' in dockerfile
