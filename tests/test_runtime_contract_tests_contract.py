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


def test_runtime_optional_task_contract_uses_current_execute_payload_fields():
    text = (RCT / 'test_optional_chat_task_contract.py').read_text(encoding='utf-8')
    assert 'task_id' in text
    assert 'input_payload' in text
    assert 'session_id' in text
    assert 'task_input' not in text
    assert 'portal_session_id' not in text


def test_runtime_optional_task_contract_cleans_up_running_tasks():
    text = (RCT / 'test_optional_chat_task_contract.py').read_text(encoding='utf-8')
    assert '/api/tasks/{task_id}/cancel' in text or '/cancel' in text
    assert 'accepted' in text and 'running' in text


def test_runtime_contract_tests_do_not_expect_external_tools():
    text = _read_all_contract_tests()
    forbidden = [
        "RUNTIME_CONTRACT_EXPECT_LEGACY_TOOL",
        "RUNTIME_CONTRACT_EXPECT_OPENCODE_TOOL",
        "RUNTIME_CONTRACT_EXPECT_TOOL_MAPPING",
        "RUNTIME_CONTRACT_EXPECT_TOOL",
        "RUNTIME_CONTRACT_EXPECT_EFP_TOOL",
        "tool_mappings",
        "opencode_tools",
        "tools-index",
        "tools_index",
        "/app/tools",
        "EFP_TOOLS_DIR",
        "OPENCODE_TOOLS_DIR",
    ]
    for token in forbidden:
        assert token not in text


def test_runtime_contract_tests_directory_has_no_removed_asset_mapping_file():
    assert not (RCT / "test_optional_smoke_asset_mapping_contract.py").exists()


def test_runtime_contract_docs_cover_empty_final_non_success():
    text = (ROOT / "docs" / "RUNTIME_CONTRACT.md").read_text(encoding="utf-8")
    assert "empty_final" in text
    assert "ok=false" in text
