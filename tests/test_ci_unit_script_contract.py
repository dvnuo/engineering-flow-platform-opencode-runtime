from pathlib import Path


def _script() -> str:
    return (Path(__file__).resolve().parents[1] / 'scripts' / 'ci_unit.sh').read_text(encoding='utf-8')


def test_ci_unit_not_appkeywarning_gate_is_targeted_not_full_suite_quiet():
    script = _script()
    assert '== NotAppKeyWarning gate ==' in script
    assert 'tests/test_app_keys.py -W error::aiohttp.web_exceptions.NotAppKeyWarning' in script
    assert 'python -m pytest -q -W error::aiohttp.web_exceptions.NotAppKeyWarning' not in script


def test_ci_unit_uses_per_step_timeouts():
    script = _script()
    assert 'PYTEST_TIMEOUT_SECONDS' in script
    assert 'FULL_PYTEST_TIMEOUT_SECONDS' in script
    assert 'run_pytest()' in script
    assert 'timeout "${timeout_seconds}" python -m pytest' in script


def test_ci_unit_keeps_required_gates():
    script = _script()
    for marker in [
        '== opencode_client leak gate ==',
        '== AppKey static/runtime gate ==',
        '== pytest config gate ==',
        '== NotAppKeyWarning gate ==',
        '== P2 subset ==',
        '== full pytest ==',
    ]:
        assert marker in script


def test_ci_unit_static_grep_covers_single_and_double_quote_app_keys():
    script = _script()
    assert 'app.get("' in script
    assert "app.get('" in script
    assert 'request.app.get("' in script
    assert "request.app.get('" in script
    assert 'app\\["' in script
    assert "app\\['" in script
    assert 'request.app\\["' in script
    assert "request.app\\['" in script
