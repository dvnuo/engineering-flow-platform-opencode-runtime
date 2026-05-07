import json
import os
import shutil

import pytest

from efp_opencode_adapter.tool_deps import ensure_tool_deps, verify_tool_dependency_resolution


def _write_vendored_plugin(vendored_dir, version="1.14.39"):
    plugin_package = vendored_dir / "node_modules" / "@opencode-ai" / "plugin" / "package.json"
    plugin_package.parent.mkdir(parents=True, exist_ok=True)
    plugin_package.write_text(
        json.dumps(
            {
                "name": "@opencode-ai/plugin",
                "version": version,
                "type": "module",
                "exports": {
                    ".": {
                        "import": "./index.js",
                        "types": "./index.d.ts",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (plugin_package.parent / "index.js").write_text(
        'import { z } from "zod";\n'
        "export function tool(input) { return input }\n"
        "tool.schema = z\n",
        encoding="utf-8",
    )
    (plugin_package.parent / "index.d.ts").write_text("export declare const tool: any;\n", encoding="utf-8")

    zod_pkg = vendored_dir / "node_modules" / "zod" / "package.json"
    zod_pkg.parent.mkdir(parents=True, exist_ok=True)
    zod_pkg.write_text(
        json.dumps(
            {
                "name": "zod",
                "version": "3.0.0",
                "type": "module",
                "exports": {".": {"import": "./index.js", "types": "./index.d.ts"}},
            }
        ),
        encoding="utf-8",
    )
    (zod_pkg.parent / "index.js").write_text("export const z = {}; export default z;\n", encoding="utf-8")
    (zod_pkg.parent / "index.d.ts").write_text("export declare const z: any;\n", encoding="utf-8")

    eff_pkg = vendored_dir / "node_modules" / "effect" / "package.json"
    eff_pkg.parent.mkdir(parents=True, exist_ok=True)
    eff_pkg.write_text(
        json.dumps(
            {
                "name": "effect",
                "version": "3.0.0",
                "type": "module",
                "exports": {".": {"import": "./index.js", "types": "./index.d.ts"}},
            }
        ),
        encoding="utf-8",
    )
    (eff_pkg.parent / "index.js").write_text("export const Effect = {};\n", encoding="utf-8")
    (eff_pkg.parent / "index.d.ts").write_text("export declare const Effect: any;\n", encoding="utf-8")


def _write_vendored_plugin_import_only_exports(vendored_dir, version="1.14.39"):
    plugin_dir = vendored_dir / "node_modules" / "@opencode-ai" / "plugin"
    dist_dir = plugin_dir / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)

    (plugin_dir / "package.json").write_text(
        json.dumps(
            {
                "name": "@opencode-ai/plugin",
                "version": version,
                "type": "module",
                "exports": {
                    ".": {
                        "import": "./dist/index.js",
                        "types": "./dist/index.d.ts",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    (dist_dir / "index.js").write_text(
        'import { z } from "zod";\n'
        'import { Effect } from "effect";\n'
        'export function tool(input) { return input }\n'
        'tool.schema = z\n'
        'export { Effect }\n',
        encoding="utf-8",
    )
    (dist_dir / "index.d.ts").write_text("export declare const tool: any;\n", encoding="utf-8")

    for name, export_code in {
        "zod": "export const z = { string: () => ({ describe() { return this } }) }; export default z;\n",
        "effect": "export const Effect = {};\n",
    }.items():
        pkg_dir = vendored_dir / "node_modules" / name
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "package.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "version": "3.0.0",
                    "type": "module",
                    "exports": {
                        ".": {
                            "import": "./index.js",
                            "types": "./index.d.ts",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        (pkg_dir / "index.js").write_text(export_code, encoding="utf-8")
        (pkg_dir / "index.d.ts").write_text("export declare const x: any;\n", encoding="utf-8")


def _write_vendored_bin_symlink(vendored_dir):
    which_bin = vendored_dir / "node_modules" / "which" / "bin" / "node-which"
    which_bin.parent.mkdir(parents=True, exist_ok=True)

    which_pkg = vendored_dir / "node_modules" / "which" / "package.json"
    which_pkg.write_text(
        json.dumps({"name": "which", "version": "5.0.0", "bin": {"node-which": "bin/node-which"}}),
        encoding="utf-8",
    )
    which_bin.write_text("#!/usr/bin/env node\n", encoding="utf-8")

    bin_dir = vendored_dir / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    os.symlink("../which/bin/node-which", bin_dir / "node-which")


def test_ensure_tool_deps_copies_plugin_and_writes_package_json(tmp_path):
    workspace = tmp_path / "workspace"
    vendored = tmp_path / "vendored"
    _write_vendored_plugin(vendored, "1.14.39")

    result = ensure_tool_deps(workspace_dir=workspace, vendored_dir=vendored)

    assert (workspace / ".opencode/node_modules/@opencode-ai/plugin/package.json").exists()
    package_json = json.loads((workspace / ".opencode/package.json").read_text(encoding="utf-8"))
    assert package_json["dependencies"]["@opencode-ai/plugin"] == "1.14.39"
    assert result["status"] == "ok"


def test_ensure_tool_deps_verifies_node_resolution(tmp_path):
    workspace = tmp_path / "workspace"
    vendored = tmp_path / "vendored"
    _write_vendored_plugin(vendored)

    result = ensure_tool_deps(workspace_dir=workspace, vendored_dir=vendored)
    assert ".opencode/node_modules/@opencode-ai/plugin" in result["resolved_plugin"]


def test_ensure_tool_deps_supports_import_only_package_exports(tmp_path):
    workspace = tmp_path / "workspace"
    vendored = tmp_path / "vendored"
    _write_vendored_plugin_import_only_exports(vendored)

    result = ensure_tool_deps(workspace_dir=workspace, vendored_dir=vendored)

    assert result["status"] == "ok"
    assert ".opencode/node_modules/@opencode-ai/plugin" in result["resolved_plugin"]
    assert ".opencode/node_modules/zod" in result["resolved_zod"]
    assert ".opencode/node_modules/effect" in result["resolved_effect"]


def test_verify_tool_dependency_resolution_includes_node_stderr_on_failure(tmp_path):
    workspace = tmp_path / "workspace"
    vendored = tmp_path / "vendored"
    _write_vendored_plugin_import_only_exports(vendored)

    shutil.rmtree(vendored / "node_modules" / "zod")

    with pytest.raises(RuntimeError) as exc:
        ensure_tool_deps(workspace_dir=workspace, vendored_dir=vendored)

    message = str(exc.value)
    assert "OpenCode custom tool dependency resolution failed" in message
    assert "stderr=" in message or "stdout=" in message


def test_ensure_tool_deps_fails_when_transitive_dep_missing(tmp_path):
    workspace = tmp_path / "workspace"
    vendored = tmp_path / "vendored"
    _write_vendored_plugin(vendored)
    zod_dir = vendored / "node_modules" / "zod"
    for child in zod_dir.glob("**/*"):
        if child.is_file():
            child.unlink()
    for child in sorted(zod_dir.glob("**/*"), reverse=True):
        if child.is_dir():
            child.rmdir()
    zod_dir.rmdir()

    with pytest.raises(RuntimeError, match="OpenCode custom tool dependency resolution failed"):
        ensure_tool_deps(workspace_dir=workspace, vendored_dir=vendored)


def test_ensure_tool_deps_does_not_create_probe_tool_file(tmp_path):
    workspace = tmp_path / "workspace"
    vendored = tmp_path / "vendored"
    _write_vendored_plugin(vendored)

    ensure_tool_deps(workspace_dir=workspace, vendored_dir=vendored)
    assert not (workspace / ".opencode/tools/__resolve_probe.ts").exists()


def test_ensure_tool_deps_preserves_existing_package_json_fields(tmp_path):
    workspace = tmp_path / "workspace"
    vendored = tmp_path / "vendored"
    _write_vendored_plugin(vendored)
    config_dir = workspace / ".opencode"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "package.json").write_text(
        json.dumps(
            {
                "name": "custom-name",
                "private": True,
                "dependencies": {"left-pad": "1.3.0"},
                "custom": {"keep": True},
            }
        ),
        encoding="utf-8",
    )

    ensure_tool_deps(workspace_dir=workspace, vendored_dir=vendored)
    payload = json.loads((config_dir / "package.json").read_text(encoding="utf-8"))

    assert payload["name"] == "custom-name"
    assert payload["custom"] == {"keep": True}
    assert payload["dependencies"]["left-pad"] == "1.3.0"
    assert payload["dependencies"]["@opencode-ai/plugin"] == "1.14.39"


def test_ensure_tool_deps_preserves_existing_node_modules_content(tmp_path):
    workspace = tmp_path / "workspace"
    vendored = tmp_path / "vendored"
    _write_vendored_plugin(vendored)
    existing = workspace / ".opencode/node_modules/some-existing/package.json"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text('{"name":"some-existing"}', encoding="utf-8")

    ensure_tool_deps(workspace_dir=workspace, vendored_dir=vendored)

    assert existing.exists()
    assert (workspace / ".opencode/node_modules/@opencode-ai/plugin/package.json").exists()


def test_ensure_tool_deps_repairs_existing_stale_package_lock(tmp_path):
    workspace = tmp_path / "workspace"
    vendored = tmp_path / "vendored"
    _write_vendored_plugin(vendored)
    lockfile = workspace / ".opencode/package-lock.json"
    lockfile.parent.mkdir(parents=True, exist_ok=True)
    lockfile.write_text(
        json.dumps({"name": "stale", "lockfileVersion": 3, "requires": True, "packages": {"": {"dependencies": {}}}}),
        encoding="utf-8",
    )

    ensure_tool_deps(workspace_dir=workspace, vendored_dir=vendored)
    payload = json.loads(lockfile.read_text(encoding="utf-8"))
    assert payload["packages"][""]["dependencies"]["@opencode-ai/plugin"] == "1.14.39"


def test_ensure_tool_deps_replaces_invalid_package_lock(tmp_path):
    workspace = tmp_path / "workspace"
    vendored = tmp_path / "vendored"
    _write_vendored_plugin(vendored)
    lockfile = workspace / ".opencode/package-lock.json"
    lockfile.parent.mkdir(parents=True, exist_ok=True)
    lockfile.write_text("{broken", encoding="utf-8")

    ensure_tool_deps(workspace_dir=workspace, vendored_dir=vendored)
    payload = json.loads(lockfile.read_text(encoding="utf-8"))
    assert payload["packages"][""]["dependencies"]["@opencode-ai/plugin"] == "1.14.39"


def test_ensure_tool_deps_fails_when_vendored_plugin_missing(tmp_path):
    with pytest.raises(RuntimeError, match="Missing vendored @opencode-ai/plugin"):
        ensure_tool_deps(workspace_dir=tmp_path / "workspace", vendored_dir=tmp_path / "vendored")


def test_ensure_tool_deps_rejects_invalid_existing_package_json(tmp_path):
    workspace = tmp_path / "workspace"
    vendored = tmp_path / "vendored"
    _write_vendored_plugin(vendored)
    package_json = workspace / ".opencode/package.json"
    package_json.parent.mkdir(parents=True, exist_ok=True)
    package_json.write_text("{invalid", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Invalid JSON"):
        ensure_tool_deps(workspace_dir=workspace, vendored_dir=vendored)


def test_ensure_tool_deps_rejects_transitive_resolution_from_node_path(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    vendored = tmp_path / "vendored"
    plugin_package = vendored / "node_modules" / "@opencode-ai" / "plugin" / "package.json"
    plugin_package.parent.mkdir(parents=True, exist_ok=True)
    plugin_package.write_text(json.dumps({"name": "@opencode-ai/plugin", "version": "1.14.39", "type": "module", "main": "index.js"}), encoding="utf-8")
    (plugin_package.parent / "index.js").write_text('export { z } from "zod"; export * as Effect from "effect";\n', encoding="utf-8")

    fake_global = tmp_path / "fake-global" / "node_modules"
    for name in ("zod", "effect"):
        pkg = fake_global / name / "package.json"
        pkg.parent.mkdir(parents=True, exist_ok=True)
        pkg.write_text(json.dumps({"name": name, "version": "9.9.9", "main": "index.js"}), encoding="utf-8")
        (pkg.parent / "index.js").write_text("module.exports = {};\n", encoding="utf-8")

    monkeypatch.setenv("NODE_PATH", str(fake_global))
    with pytest.raises(RuntimeError, match="OpenCode custom tool dependency resolution failed"):
        ensure_tool_deps(workspace_dir=workspace, vendored_dir=vendored)


def test_ensure_tool_deps_prefers_local_transitive_deps_when_node_path_is_set(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    vendored = tmp_path / "vendored"
    _write_vendored_plugin(vendored)

    fake_global = tmp_path / "fake-global" / "node_modules"
    for name in ("zod", "effect"):
        pkg = fake_global / name / "package.json"
        pkg.parent.mkdir(parents=True, exist_ok=True)
        pkg.write_text(json.dumps({"name": name, "version": "9.9.9", "main": "index.js"}), encoding="utf-8")
        (pkg.parent / "index.js").write_text("module.exports = {};\n", encoding="utf-8")

    monkeypatch.setenv("NODE_PATH", str(fake_global))
    result = ensure_tool_deps(workspace_dir=workspace, vendored_dir=vendored)
    assert ".opencode/node_modules/zod" in result["resolved_zod"]
    assert ".opencode/node_modules/effect" in result["resolved_effect"]


def test_ensure_tool_deps_replaces_previous_plugin_symlink(tmp_path):
    workspace = tmp_path / "workspace"
    vendored = tmp_path / "vendored"
    _write_vendored_plugin(vendored)

    global_plugin = tmp_path / "global-plugin"
    global_plugin.mkdir(parents=True, exist_ok=True)
    (global_plugin / "package.json").write_text(json.dumps({"name": "@opencode-ai/plugin", "version": "old"}), encoding="utf-8")

    workspace_plugin = workspace / ".opencode" / "node_modules" / "@opencode-ai" / "plugin"
    workspace_plugin.parent.mkdir(parents=True, exist_ok=True)
    workspace_plugin.symlink_to(global_plugin, target_is_directory=True)

    result = ensure_tool_deps(workspace_dir=workspace, vendored_dir=vendored)

    assert not workspace_plugin.is_symlink()
    assert (workspace_plugin / "package.json").exists()
    assert ".opencode/node_modules/@opencode-ai/plugin" in result["resolved_plugin"]
    global_payload = json.loads((global_plugin / "package.json").read_text(encoding="utf-8"))
    assert global_payload["version"] == "old"


def test_ensure_tool_deps_replaces_previous_opencode_scope_symlink(tmp_path):
    workspace = tmp_path / "workspace"
    vendored = tmp_path / "vendored"
    _write_vendored_plugin(vendored)

    global_scope = tmp_path / "global-scope" / "@opencode-ai"
    (global_scope / "plugin").mkdir(parents=True, exist_ok=True)
    (global_scope / "plugin/package.json").write_text(
        json.dumps({"name": "@opencode-ai/plugin", "version": "old"}),
        encoding="utf-8",
    )

    workspace_scope = workspace / ".opencode/node_modules/@opencode-ai"
    workspace_scope.parent.mkdir(parents=True, exist_ok=True)
    workspace_scope.symlink_to(global_scope, target_is_directory=True)

    result = ensure_tool_deps(workspace_dir=workspace, vendored_dir=vendored)

    assert not workspace_scope.is_symlink()
    assert (workspace_scope / "plugin/package.json").exists()
    assert ".opencode/node_modules/@opencode-ai/plugin" in result["resolved_plugin"]

    global_payload = json.loads((global_scope / "plugin/package.json").read_text(encoding="utf-8"))
    assert global_payload["version"] == "old"


def test_ensure_tool_deps_is_idempotent_with_vendored_bin_symlink(tmp_path):
    workspace = tmp_path / "workspace"
    vendored = tmp_path / "vendored"
    _write_vendored_plugin(vendored)
    _write_vendored_bin_symlink(vendored)

    first = ensure_tool_deps(workspace_dir=workspace, vendored_dir=vendored)
    second = ensure_tool_deps(workspace_dir=workspace, vendored_dir=vendored)

    assert first["status"] == "ok"
    assert second["status"] == "ok"

    local_bin = workspace / ".opencode" / "node_modules" / ".bin" / "node-which"
    assert local_bin.is_symlink()
    assert os.readlink(local_bin) == "../which/bin/node-which"


def test_ensure_tool_deps_replaces_existing_vendored_bin_symlink(tmp_path):
    workspace = tmp_path / "workspace"
    vendored = tmp_path / "vendored"
    _write_vendored_plugin(vendored)
    _write_vendored_bin_symlink(vendored)

    existing_bin = workspace / ".opencode" / "node_modules" / ".bin" / "node-which"
    existing_bin.parent.mkdir(parents=True, exist_ok=True)
    existing_bin.symlink_to("../old/bin/node-which")

    result = ensure_tool_deps(workspace_dir=workspace, vendored_dir=vendored)

    assert result["status"] == "ok"
    assert existing_bin.is_symlink()
    assert os.readlink(existing_bin) == "../which/bin/node-which"


def test_ensure_tool_deps_preserves_unrelated_existing_bin_entry(tmp_path):
    workspace = tmp_path / "workspace"
    vendored = tmp_path / "vendored"
    _write_vendored_plugin(vendored)
    _write_vendored_bin_symlink(vendored)

    custom_bin = workspace / ".opencode" / "node_modules" / ".bin" / "custom-existing"
    custom_bin.parent.mkdir(parents=True, exist_ok=True)
    custom_bin.write_text("#!/usr/bin/env node\n", encoding="utf-8")

    ensure_tool_deps(workspace_dir=workspace, vendored_dir=vendored)

    assert custom_bin.exists()
    assert (workspace / ".opencode/node_modules/.bin/node-which").is_symlink()


def test_ensure_tool_deps_replaces_previous_node_modules_root_symlink(tmp_path):
    workspace = tmp_path / "workspace"
    vendored = tmp_path / "vendored"
    _write_vendored_plugin(vendored)
    _write_vendored_bin_symlink(vendored)

    global_node_modules = tmp_path / "global" / "node_modules"
    global_node_modules.mkdir(parents=True, exist_ok=True)

    old_plugin_pkg = global_node_modules / "@opencode-ai" / "plugin" / "package.json"
    old_plugin_pkg.parent.mkdir(parents=True, exist_ok=True)
    old_plugin_pkg.write_text(json.dumps({"name": "@opencode-ai/plugin", "version": "old"}), encoding="utf-8")

    workspace_node_modules = workspace / ".opencode" / "node_modules"
    workspace_node_modules.parent.mkdir(parents=True, exist_ok=True)
    workspace_node_modules.symlink_to(global_node_modules, target_is_directory=True)

    result = ensure_tool_deps(workspace_dir=workspace, vendored_dir=vendored)

    assert not workspace_node_modules.is_symlink()
    assert workspace_node_modules.is_dir()
    assert (workspace_node_modules / "@opencode-ai/plugin/package.json").exists()
    assert (workspace_node_modules / "zod/package.json").exists()
    assert (workspace_node_modules / "effect/package.json").exists()

    assert ".opencode/node_modules/@opencode-ai/plugin" in result["resolved_plugin"]
    assert ".opencode/node_modules/zod" in result["resolved_zod"]
    assert ".opencode/node_modules/effect" in result["resolved_effect"]

    global_payload = json.loads(old_plugin_pkg.read_text(encoding="utf-8"))
    assert global_payload["version"] == "old"

    local_bin = workspace_node_modules / ".bin" / "node-which"
    assert local_bin.is_symlink()
    assert os.readlink(local_bin) == "../which/bin/node-which"


def test_verify_tool_dependency_resolution_rejects_node_modules_root_symlink(tmp_path):
    config_dir = tmp_path / "workspace" / ".opencode"
    external = tmp_path / "global" / "node_modules"
    _write_vendored_plugin(external.parent)

    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "node_modules").symlink_to(external, target_is_directory=True)

    with pytest.raises(RuntimeError, match="OpenCode custom tool dependency resolution failed"):
        verify_tool_dependency_resolution(config_dir)
