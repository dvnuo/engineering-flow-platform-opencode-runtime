from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_pytest_trace_config_does_not_register_ddtrace_plugins():
    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            '-m',
            'pytest',
            '--trace-config',
            '-q',
            'tests/test_pytest_config.py',
        ],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr

    assert 'ddtrace.contrib.pytest' not in combined
    assert 'ddtrace/vendor/psutil' not in combined
