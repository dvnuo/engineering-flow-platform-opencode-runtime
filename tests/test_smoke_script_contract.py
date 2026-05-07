from pathlib import Path


def _script() -> str:
    return (Path(__file__).resolve().parents[1] / 'scripts' / 'smoke.sh').read_text(encoding='utf-8')


def _call_count(script: str, name: str) -> int:
    return sum(1 for line in script.splitlines() if line.strip() == name)


def test_smoke_script_asserts_skill_tool_mapping_contract():
    script = _script()
    assert 'legacy_name' in script
    assert 'smoke_tool' in script
    assert 'efp_smoke_tool' in script
    assert 'smoke_tool -> efp_smoke_tool' in script
    assert 'opencode_tools' in script
    assert 'tool_mappings' in script


def test_smoke_script_uses_real_plugin_tool_wrapper():
    script = _script()
    assert 'import { tool } from "@opencode-ai/plugin"' in script
    assert 'export default tool({' in script
    assert 'async execute(args, context)' in script
    assert 'tool.schema.string()' in script
    assert 'export default async function efp_smoke_tool()' not in script


def test_smoke_script_asserts_local_opencode_plugin_dependency():
    script = _script()
    assert '/workspace/.opencode/node_modules/@opencode-ai/plugin/package.json' in script


def test_smoke_script_forces_opencode_tool_registry_import():
    script = _script()
    assert 'efp_smoke_tool' in script
    assert "tool_registry_check" in script


def test_smoke_script_can_run_runtime_contract_tests():
    script = _script()
    assert 'RUN_RUNTIME_CONTRACT_TESTS' in script
    assert 'RUNTIME_CONTRACT_BASE_URL' in script
    assert 'RUNTIME_BASE_URL=' in script
    assert 'python -m pytest -q runtime_contract_tests' in script


def test_smoke_script_does_not_enable_live_llm_contracts_by_default():
    script = _script()
    assert 'RUNTIME_CONTRACT_ENABLE_CHAT=1' not in script
    assert 'RUNTIME_CONTRACT_ENABLE_TASKS=1' not in script


def test_smoke_script_runs_contract_tests_after_restart():
    script = _script()
    assert _call_count(script, "run_runtime_contract_tests") >= 2
    assert 'docker restart' in script


def test_smoke_script_dumps_docker_logs_on_failure():
    script = _script()
    assert 'docker logs' in script
    assert 'dump_logs_on_failure' in script


def test_smoke_script_passes_runtime_contract_expected_asset_env_and_timeout():
    script = _script()
    assert 'RUNTIME_CONTRACT_TIMEOUT_SECONDS' in script
    assert 'RUNTIME_CONTRACT_EXPECT_SKILL' in script
    assert 'RUNTIME_CONTRACT_EXPECT_LEGACY_TOOL' in script
    assert 'RUNTIME_CONTRACT_EXPECT_OPENCODE_TOOL' in script
    assert 'RUNTIME_CONTRACT_EXPECT_TOOL_MAPPING' in script
    assert 'RUNTIME_CONTRACT_EXPECT_TOOL' in script
    assert 'RUNTIME_CONTRACT_EXPECT_EFP_TOOL' in script
    assert '${RUNTIME_CONTRACT_EXPECT_LEGACY_TOOL}:${RUNTIME_CONTRACT_EXPECT_OPENCODE_TOOL}' in script
    assert 'timeout "${RUNTIME_CONTRACT_TIMEOUT_SECONDS}" env' in script


def test_smoke_script_uses_root_state_and_asserts_root_user():
    script = _script()
    assert "/root/.local/share/opencode" in script
    assert "/root/.local/share/efp-compat" in script
    assert "/home/opencode" not in script
    assert "id -u" in script


def test_smoke_script_validates_node_resolution_helpers():
    script = _script()
    assert "assert_node_tool_dependency_resolution" in script
    assert 'import.meta.resolve("@opencode-ai/plugin")' in script
    assert 'import.meta.resolve("zod")' in script
    assert 'import.meta.resolve("effect")' in script
    assert 'await import("@opencode-ai/plugin")' in script
    assert "createRequire" not in script
    assert 'req.resolve("@opencode-ai/plugin")' not in script


def test_smoke_script_checks_registry_on_first_start_and_restart():
    script = _script()

    assert _call_count(script, "assert_node_tool_dependency_resolution") >= 2
    assert _call_count(script, "assert_opencode_binary_version") >= 2
    assert _call_count(script, "assert_opencode_tool_registry") >= 2
    assert _call_count(script, "assert_workspace_package_lock_declares_plugin") >= 2

    first_segment = script[
        script.index("wait_health"):
        script.index("docker exec \"${NAME}\" sh -lc 'echo adapter-persist")
    ]
    assert "assert_node_tool_dependency_resolution" in first_segment
    assert "assert_opencode_binary_version" in first_segment
    assert "assert_opencode_tool_registry" in first_segment
    assert "assert_workspace_package_lock_declares_plugin" in first_segment

    restart_segment = script[script.index("docker restart"):]
    assert "assert_node_tool_dependency_resolution" in restart_segment
    assert "assert_opencode_binary_version" in restart_segment
    assert "assert_opencode_tool_registry" in restart_segment
    assert "assert_workspace_package_lock_declares_plugin" in restart_segment


def test_smoke_script_asserts_stale_lock_repaired():
    script = _script()
    assert "stale-opencode-workspace" in script
    assert "assert_workspace_package_lock_declares_plugin" in script
    assert '.packages[""].dependencies["@opencode-ai/plugin"]' in script or "@opencode-ai/plugin" in script


def test_smoke_script_asserts_plugin_zod_effect_realpaths_are_workspace_local():
    script = _script()
    assert 'fs.realpathSync' in script
    assert 'assertLocal("plugin", plugin)' in script
    assert 'assertLocal("zod", zod)' in script
    assert 'assertLocal("effect", effect)' in script
    assert '/workspace/.opencode/node_modules' in script
    assert 'resolved outside workspace .opencode node_modules' in script


def test_smoke_script_preseeds_and_repairs_node_modules_root_symlink():
    script = _script()
    assert "global-node-modules" in script
    assert "../global-node-modules" in script
    assert "assert_workspace_node_modules_is_local_directory" in script
    assert _call_count(script, "assert_workspace_node_modules_is_local_directory") >= 2
    assert "test ! -L /workspace/.opencode/node_modules" in script


def test_smoke_script_builds_with_explicit_opencode_version_build_arg_and_checks_version():
    script = _script()
    assert '--build-arg "OPENCODE_VERSION=${OPENCODE_VERSION}"' in script
    assert "assert_opencode_binary_version" in script
    assert "opencode --version" in script
    assert "/app/runtime/package.json" in script
    
def test_smoke_script_does_not_reference_removed_internal_server_credential_names():
    script = _script()
    prefix = "OPENCODE_" + "SERVER_"
    forbidden = [prefix + "USERNAME", prefix + "PASSWORD"]
    for token in forbidden:
        assert token not in script
