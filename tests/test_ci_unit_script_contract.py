from pathlib import Path


def test_ci_unit_not_appkeywarning_gate_is_targeted_not_full_suite_quiet():
    script = (Path(__file__).resolve().parents[1] / 'scripts' / 'ci_unit.sh').read_text(encoding='utf-8')
    assert '== NotAppKeyWarning gate ==' in script
    assert 'python -m pytest -q tests/test_app_keys.py -W error::aiohttp.web_exceptions.NotAppKeyWarning' in script
    assert 'python -m pytest -q -W error::aiohttp.web_exceptions.NotAppKeyWarning' not in script
