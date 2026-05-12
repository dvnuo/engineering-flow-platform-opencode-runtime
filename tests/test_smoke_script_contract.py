from pathlib import Path


def _script() -> str:
    return (Path(__file__).resolve().parents[1] / 'scripts' / 'smoke.sh').read_text(encoding='utf-8')


def test_smoke_script_runs_health_skills_capabilities_and_optional_contract_tests():
    script = _script()
    assert 'curl -fsS http://localhost:8000/health' in script
    assert 'curl -fsS http://localhost:8000/api/skills' in script
    assert 'curl -fsS http://localhost:8000/api/capabilities' in script
    assert 'RUN_RUNTIME_CONTRACT_TESTS' in script
    assert 'python -m pytest -q runtime_contract_tests' in script


def test_smoke_script_does_not_reference_removed_external_tool_contract_knobs():
    script = _script()
    forbidden = [
        'RUNTIME_CONTRACT_EXPECT_LEGACY_TOOL',
        'RUNTIME_CONTRACT_EXPECT_OPENCODE_TOOL',
        'RUNTIME_CONTRACT_EXPECT_TOOL_MAPPING',
        'RUNTIME_CONTRACT_EXPECT_TOOL',
        'RUNTIME_CONTRACT_EXPECT_EFP_TOOL',
        'tool_mappings',
        'opencode_tools',
        'tool_mapping',
        'tools-index',
        '/app/tools',
        'EFP_TOOLS_DIR',
        'OPENCODE_TOOLS_DIR',
    ]
    for token in forbidden:
        assert token not in script
