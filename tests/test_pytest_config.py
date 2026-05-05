from __future__ import annotations

import tomllib
from pathlib import Path


def test_pytest_disables_external_ddtrace_plugin_by_default():
    pyproject = Path(__file__).resolve().parents[1] / 'pyproject.toml'
    data = tomllib.loads(pyproject.read_text(encoding='utf-8'))
    addopts = data['tool']['pytest']['ini_options'].get('addopts', [])
    assert '-p' in addopts
    assert 'no:ddtrace' in addopts
