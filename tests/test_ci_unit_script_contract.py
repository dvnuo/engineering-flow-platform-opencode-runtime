from pathlib import Path


def _script() -> str:
    return (Path(__file__).resolve().parents[1] / 'scripts' / 'ci_unit.sh').read_text(encoding='utf-8')


def test_ci_unit_not_appkeywarning_gate_is_targeted_not_full_suite_quiet():
    script = _script()
    assert 'run_pytest_gate "NotAppKeyWarning gate"' in script
    assert 'tests/test_app_keys.py -W error::aiohttp.web_exceptions.NotAppKeyWarning' in script
    assert 'python -m pytest -q -W error::aiohttp.web_exceptions.NotAppKeyWarning' not in script


def test_ci_unit_uses_per_step_timeouts_and_kill_after():
    script = _script()
    assert 'PYTEST_TIMEOUT_SECONDS' in script
    assert 'FULL_PYTEST_TIMEOUT_SECONDS' in script
    assert 'PYTEST_KILL_AFTER_SECONDS' in script
    assert 'run_pytest()' in script
    assert 'timeout --kill-after' in script
    assert '${PYTEST_KILL_AFTER_SECONDS}' in script


def test_ci_unit_writes_per_gate_logs_and_tails_on_failure():
    script = _script()
    assert 'CI_LOG_DIR' in script
    assert 'mkdir -p' in script
    assert '>"${log_file}" 2>&1' in script
    assert 'cat "${log_file}"' in script
    assert 'tail -120' in script
    assert 'failed; tail of' in script
    assert '| tee' not in script


def test_ci_unit_keeps_required_gates():
    script = _script()
    for marker in [
        'run_pytest_gate "opencode_client leak gate"',
        'run_pytest_gate "AppKey static/runtime gate"',
        'run_pytest_gate "pytest config gate"',
        'run_pytest_gate "runtime contract default skip/import gate"',
        'run_pytest_gate "NotAppKeyWarning gate"',
        '== P2 subset gates ==',
        'run_pytest_gate "P2 chat stream/recovery subset"',
        'run_pytest_gate "P2 skill/assets/capabilities subset"',
        'run_pytest_gate "P2 event/profile subset"',
        'run_pytest_gate "P2 tasks/recovery/tools subset"',
        'run_pytest_gate "full pytest"',
    ]:
        assert marker in script


def test_ci_unit_splits_p2_subset_by_domain():
    script = _script()
    assert 'P2 chat stream/recovery subset' in script
    assert 'tests/test_chat_streaming.py' in script
    assert 'tests/test_chat_recovery_hardening.py' in script

    assert 'P2 skill/assets/capabilities subset' in script
    assert 'tests/test_skill_sync.py' in script
    assert 'tests/test_capabilities_api.py' in script
    assert 'tests/test_init_assets.py' in script

    assert 'P2 event/profile subset' in script
    assert 'tests/test_event_bridge.py' in script
    assert 'tests/test_runtime_profile_apply.py' in script

    assert 'P2 tasks/recovery/tools subset' in script
    assert 'tests/test_tasks_api.py' in script
    assert 'tests/test_recovery.py' in script
    assert 'tests/test_no_custom_tool_deps_contract.py' in script


def test_ci_unit_runs_runtime_contract_default_skip_gate():
    script = _script()
    assert 'runtime contract default skip/import gate' in script
    assert 'runtime_contract_tests' in script
    assert 'RUNTIME_CONTRACT_ENABLE_CHAT=1' not in script
    assert 'RUNTIME_CONTRACT_ENABLE_TASKS=1' not in script


def test_ci_unit_prints_pytest_commands_for_diagnostics():
    script = _script()
    assert '+ timeout --kill-after' in script
    assert 'python -m pytest' in script
    assert 'pytest gate failed or timed out' in script


def test_ci_unit_appkey_static_scan_uses_python_source_only_helper_and_keeps_patterns():
    script = _script()
    assert 'assert_no_adapter_py_source_matches()' in script
    assert 'find efp_opencode_adapter' in script
    assert "-type f" in script
    assert "-name '*.py'" in script
    assert "! -path '*/__pycache__/*'" in script
    assert 'grep -n -F -- "${pattern}"' in script

    assert 'grep -R \'app.get("\' -n efp_opencode_adapter' not in script
    assert 'grep -R "app.get(\'" -n efp_opencode_adapter' not in script
    assert 'grep -R \'request.app.get("\' -n efp_opencode_adapter' not in script
    assert 'grep -R \'app\["\' -n efp_opencode_adapter' not in script

    for token in [
        'from .app_keys import *',
        'app.get("',
        "app.get('",
        'request.app.get("',
        "request.app.get('",
        'app["',
        "app['",
        'request.app["',
        "request.app['",
    ]:
        assert token in script

    assert 'AppKey static/runtime gate' in script
    assert 'tests/test_app_keys.py' in script

def test_ci_unit_does_not_use_tee_pipeline_for_pytest_gates():
    script = _script()
    assert '| tee' not in script
    assert 'PIPESTATUS' not in script
