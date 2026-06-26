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


def test_smoke_script_checks_atlassian_cli_schema_commands():
    script = _script()
    required = [
        'jira version --json',
        'confluence version --json',
        'mobile-auto version --json',
        'jira commands --json',
        'mobile-auto commands --json',
        'jira schema issue.map-csv --json',
        'jira schema issue.bulk-create --json',
        'mobile-auto schema run.start --json',
    ]
    for token in required:
        assert token in script


def test_smoke_script_prepares_and_validates_java_maven_runtime():
    script = _script()
    for token in [
        "prepare_maven_settings()",
        "runtime-maven",
        "SMOKE_CREATED_MAVEN_SETTINGS=1",
        'rm -f "${MAVEN_SETTINGS_PATH}"',
        '--build-arg MAVEN_SETTINGS_DIR="${MAVEN_SETTINGS_DIR}"',
        'docker exec "${NAME}" java -version',
        'docker exec "${NAME}" javac -version',
        'docker exec "${NAME}" mvn -v',
        'docker exec "${NAME}" jdk list',
        'docker exec "${NAME}" jdk current',
        'docker exec "${NAME}" jdk 21 java -version',
        'docker exec "${NAME}" mvn-jdk -v',
        'docker exec "${NAME}" mvn-jdk 21 -v',
        'docker exec "${NAME}" test -f /root/.m2/settings.xml',
        'docker exec "${NAME}" test -f /root/.m2/toolchains.xml',
        'stat -c %a /root/.m2/settings.xml',
        'stat -c %a /root/.m2/toolchains.xml',
    ]:
        assert token in script
    for token in [
        "mvn-jdk 8 -v",
        "mvn-jdk 17 -v",
        "mvn-jdk 25 -v",
        "jdk 8 java -version",
        "jdk 17 java -version",
        "jdk 25 java -version",
    ]:
        assert token not in script


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
