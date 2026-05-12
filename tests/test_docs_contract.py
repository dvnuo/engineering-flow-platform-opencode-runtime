from pathlib import Path


def _t(path: str) -> str:
    return Path(path).read_text(encoding='utf-8')


def test_docs_contract_sections_and_keywords():
    runtime = _t('docs/RUNTIME_CONTRACT.md')
    for section in [
        '## Overview', '## Runtime topology', '## Non-goals', '## Required environment variables',
        '## Required mounted directories', '## Portal-facing endpoints', '## Internal-only OpenCode server',
        '## Skills asset mapping', '## State persistence contract',
        '## Runtime profile apply/status contract', '## Runtime contract tests', '## Live LLM checks are opt-in',
        '## Failure modes and expected status',
    ]:
        assert section in runtime
    for token in [':8000', ':4096', 'Portal only calls adapter', 'must not be exposed', '/root/.local/share/opencode', '/root/.local/share/efp-compat', 'runtime-only', '/api/tasks/execute', '/api/capabilities', '/api/skills', '/workspace/.opencode/skills', 'Portal provides skills only', 'External tools subsystem removed / not supported']:
        assert token in runtime
    for forbidden in ['## Tools asset mapping', 'tools-index', 'tools_index', '/app/tools', 'EFP_TOOLS_DIR', 'OPENCODE_TOOLS_DIR', 'tool_mapping', 'opencode_tools', 'RUNTIME_CONTRACT_EXPECT_LEGACY_TOOL', 'RUNTIME_CONTRACT_EXPECT_OPENCODE_TOOL', 'RUNTIME_CONTRACT_EXPECT_TOOL_MAPPING', 'RUNTIME_CONTRACT_EXPECT_EFP_TOOL']:
        assert forbidden not in runtime

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
    integration = _t('integration/README.md')
    for token in ['No Portal startup required', 'RUN_RUNTIME_CONTRACT_TESTS', 'RUNTIME_CONTRACT_EXPECT_SKILL', 'RUNTIME_CONTRACT_ENABLE_CHAT', 'RUNTIME_CONTRACT_ENABLE_TASKS', 'No real LLM key required for default smoke']:
        assert token in integration
    for forbidden in ['RUNTIME_CONTRACT_EXPECT_LEGACY_TOOL', 'RUNTIME_CONTRACT_EXPECT_OPENCODE_TOOL', 'RUNTIME_CONTRACT_EXPECT_TOOL_MAPPING', 'RUNTIME_CONTRACT_EXPECT_TOOL', 'RUNTIME_CONTRACT_EXPECT_EFP_TOOL', 'tool_mapping', 'tool_mappings', 'opencode_tools', 'missing_tools', 'missing_opencode_tools', 'tools-index', 'tools_index', '/app/tools', 'EFP_TOOLS_DIR', 'OPENCODE_TOOLS_DIR', 'skill/tool asset bridge', 'Tools asset mapping']:
        assert forbidden not in integration
