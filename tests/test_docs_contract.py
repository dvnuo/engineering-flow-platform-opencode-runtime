from pathlib import Path


def _t(path: str) -> str:
    return Path(path).read_text(encoding='utf-8')


def test_docs_contract_sections_and_keywords():
    runtime = _t('docs/RUNTIME_CONTRACT.md')
    for section in [
        '## Overview', '## Runtime topology', '## Non-goals', '## Required environment variables',
        '## Required mounted directories', '## Portal-facing endpoints', '## Internal-only OpenCode server',
        '## Skills asset mapping', '## Tools asset mapping', '## State persistence contract',
        '## Runtime profile apply/status contract', '## Runtime contract tests', '## Live LLM checks are opt-in',
        '## Failure modes and expected status',
    ]:
        assert section in runtime
    for token in [':8000', ':4096', 'Portal only calls adapter', 'must not be exposed', '/root/.local/share/opencode', '/root/.local/share/efp-compat', 'runtime-only', '/api/tasks/execute', '/api/capabilities']:
        assert token in runtime

    obs = _t('docs/OBSERVABILITY.md')
    for section in ['## RuntimeEvent schema', '## trace_context schema', '## trace_id precedence', '## EventBus filter keys', '## Chat events', '## Task events', '## Permission events', '## Tool events', '## OpenCode raw event normalization', '## Secret redaction rules', '## Portal subscription guidance', '## Limitations', '## JSON example']:
        assert section in obs
    for token in ['trace_id', 'agent_id', 'request_id', 'task_id', 'tool_source', 'data.trace_context']:
        assert token in obs

    testing = _t('docs/TESTING.md')
    for section in ['## Local unit tests', '## CI unit script', '## Runtime contract tests', '## Docker smoke', '## Runtime-only vs Portal E2E', '## Packaging install check', '## Live LLM opt-in', '## Troubleshooting']:
        assert section in testing
    for token in ['scripts/ci_unit.sh', 'scripts/smoke.sh', 'RUN_RUNTIME_CONTRACT_TESTS', 'RUNTIME_CONTRACT_ENABLE_CHAT', 'RUNTIME_CONTRACT_ENABLE_TASKS', 'pip install -e ".[test]"']:
        assert token in testing

    readme = _t('README.md')
    for token in ['docs/RUNTIME_CONTRACT.md', 'docs/OBSERVABILITY.md', 'docs/TESTING.md']:
        assert token in readme
    assert 'No Portal startup required' in _t('integration/README.md')
