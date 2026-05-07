from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

PLUGIN_NAME = "@opencode-ai/plugin"


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected JSON object in {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _managed_dependency_paths(dst_node_modules: Path) -> list[Path]:
    return [
        dst_node_modules / "@opencode-ai" / "plugin",
        dst_node_modules / "zod",
        dst_node_modules / "effect",
    ]


def _remove_managed_dependency_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    if path.exists():
        shutil.rmtree(path)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    if path.exists():
        shutil.rmtree(path)


def _replace_from_vendored(src: Path, dst: Path) -> None:
    _remove_path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if src.is_symlink():
        os.symlink(os.readlink(src), dst)
        try:
            shutil.copystat(src, dst, follow_symlinks=False)
        except OSError:
            pass
        return

    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True)
        return

    shutil.copy2(src, dst, follow_symlinks=False)


def _sync_vendored_node_modules(src_node_modules: Path, dst_node_modules: Path) -> None:
    dst_node_modules.mkdir(parents=True, exist_ok=True)

    for src_entry in src_node_modules.iterdir():
        dst_entry = dst_node_modules / src_entry.name

        if src_entry.name == ".bin" and src_entry.is_dir() and not src_entry.is_symlink():
            if dst_entry.is_symlink() or dst_entry.is_file():
                dst_entry.unlink(missing_ok=True)
            dst_entry.mkdir(parents=True, exist_ok=True)
            for bin_entry in src_entry.iterdir():
                _replace_from_vendored(bin_entry, dst_entry / bin_entry.name)
            continue

        if src_entry.name.startswith("@") and src_entry.is_dir() and not src_entry.is_symlink():
            if dst_entry.is_symlink() or dst_entry.is_file():
                dst_entry.unlink(missing_ok=True)
            dst_entry.mkdir(parents=True, exist_ok=True)
            for scoped_entry in src_entry.iterdir():
                _replace_from_vendored(scoped_entry, dst_entry / scoped_entry.name)
            continue

        _replace_from_vendored(src_entry, dst_entry)


def _ensure_lock_declares_plugin(config_dir: Path, vendored_dir: Path, plugin_version: str) -> None:
    src_lock = vendored_dir / "package-lock.json"
    dst_lock = config_dir / "package-lock.json"

    lock_payload: dict[str, Any] | None = None
    if dst_lock.exists():
        try:
            loaded = json.loads(dst_lock.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                lock_payload = loaded
        except json.JSONDecodeError:
            lock_payload = None

    if lock_payload is None:
        if src_lock.exists():
            shutil.copy2(src_lock, dst_lock)
            lock_payload = _read_json_object(dst_lock)
        else:
            lock_payload = {
                "name": "efp-opencode-workspace-tools",
                "lockfileVersion": 3,
                "requires": True,
                "packages": {"": {"dependencies": {}}},
            }

    packages = lock_payload.get("packages")
    if not isinstance(packages, dict):
        packages = {}
    root_pkg = packages.get("")
    if not isinstance(root_pkg, dict):
        root_pkg = {}
    deps = root_pkg.get("dependencies")
    if not isinstance(deps, dict):
        deps = {}
    deps[PLUGIN_NAME] = plugin_version
    root_pkg["dependencies"] = deps
    packages[""] = root_pkg
    lock_payload["packages"] = packages

    lock_payload.setdefault("name", "efp-opencode-workspace-tools")
    lock_payload.setdefault("lockfileVersion", 3)
    lock_payload.setdefault("requires", True)

    _write_json(dst_lock, lock_payload)


def verify_tool_dependency_resolution(config_dir: Path) -> dict[str, str]:
    probe_file = config_dir / "tools" / "__resolve_probe.ts"
    script = """
const { createRequire } = require("module")
const probeFile = process.env.EFP_OPENCODE_TOOL_RESOLVE_PROBE
const req = createRequire(probeFile)
const pluginPath = req.resolve("@opencode-ai/plugin")
const pluginReq = createRequire(pluginPath)
const zodPath = pluginReq.resolve("zod")
const effectPath = pluginReq.resolve("effect")
console.log(JSON.stringify({ plugin: pluginPath, zod: zodPath, effect: effectPath }))
"""
    try:
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env={**os.environ, "EFP_OPENCODE_TOOL_RESOLVE_PROBE": str(probe_file)},
        )
    except Exception as exc:
        raise RuntimeError("OpenCode custom tool dependency resolution failed") from exc

    if result.returncode != 0:
        raise RuntimeError("OpenCode custom tool dependency resolution failed")

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("OpenCode custom tool dependency resolution failed") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("OpenCode custom tool dependency resolution failed")

    configured_node_modules = config_dir / "node_modules"
    if configured_node_modules.is_symlink() or configured_node_modules.is_file():
        raise RuntimeError(
            "OpenCode custom tool dependency resolution failed: "
            f"{configured_node_modules} is not a local directory"
        )

    configured_node_modules_resolved = configured_node_modules.resolve()
    config_dir_resolved = config_dir.resolve()
    if not str(configured_node_modules_resolved).startswith(str(config_dir_resolved) + os.sep):
        raise RuntimeError(
            "OpenCode custom tool dependency resolution failed: "
            f"{configured_node_modules} resolves outside {config_dir}"
        )

    expected_prefix = str(configured_node_modules_resolved) + os.sep
    resolved_paths: dict[str, str] = {}
    for label, value in {
        "plugin": payload.get("plugin"),
        "zod": payload.get("zod"),
        "effect": payload.get("effect"),
    }.items():
        if not isinstance(value, str) or not value:
            raise RuntimeError("OpenCode custom tool dependency resolution failed")
        resolved = str(Path(value).resolve())
        if not resolved.startswith(expected_prefix):
            raise RuntimeError(
                "OpenCode custom tool dependency resolution failed: "
                f"{label} resolved outside {configured_node_modules_resolved}"
            )
        resolved_paths[label] = resolved

    return {
        "resolved_plugin": resolved_paths["plugin"],
        "resolved_zod": resolved_paths["zod"],
        "resolved_effect": resolved_paths["effect"],
    }


def ensure_tool_deps(
    *,
    workspace_dir: Path,
    vendored_dir: Path,
    opencode_version: str | None = None,
) -> dict[str, str]:
    config_dir = workspace_dir / ".opencode"
    src_node_modules = vendored_dir / "node_modules"
    src_plugin_package = src_node_modules / "@opencode-ai" / "plugin" / "package.json"

    if not src_plugin_package.exists():
        raise RuntimeError(
            f"Missing vendored @opencode-ai/plugin: expected {src_plugin_package} "
            f"under vendored_dir={vendored_dir}"
        )

    plugin_payload = _read_json_object(src_plugin_package)
    plugin_version = str(plugin_payload.get("version") or opencode_version or "*")

    config_dir.mkdir(parents=True, exist_ok=True)
    dst_node_modules = config_dir / "node_modules"
    if dst_node_modules.is_symlink() or dst_node_modules.is_file():
        dst_node_modules.unlink(missing_ok=True)
    dst_node_modules.mkdir(parents=True, exist_ok=True)

    opencode_scope = dst_node_modules / "@opencode-ai"
    if opencode_scope.is_symlink() or opencode_scope.is_file():
        opencode_scope.unlink(missing_ok=True)
    opencode_scope.mkdir(parents=True, exist_ok=True)

    for managed_path in _managed_dependency_paths(dst_node_modules):
        _remove_managed_dependency_path(managed_path)

    try:
        _sync_vendored_node_modules(src_node_modules, dst_node_modules)
    except Exception as exc:
        raise RuntimeError("OpenCode custom tool dependency materialization failed") from exc

    local_plugin_package = dst_node_modules / "@opencode-ai" / "plugin" / "package.json"
    if not local_plugin_package.exists():
        raise RuntimeError(f"Failed to materialize @opencode-ai/plugin at {local_plugin_package}")

    package_json_path = config_dir / "package.json"
    if package_json_path.exists():
        package_json = _read_json_object(package_json_path)
    else:
        package_json = {}

    package_json.setdefault("name", "efp-opencode-workspace-tools")
    package_json["private"] = True
    package_json.setdefault("type", "module")

    deps = package_json.get("dependencies")
    if not isinstance(deps, dict):
        deps = {}
    deps[PLUGIN_NAME] = plugin_version
    package_json["dependencies"] = deps

    _write_json(package_json_path, package_json)
    _ensure_lock_declares_plugin(config_dir, vendored_dir, plugin_version)
    resolution = verify_tool_dependency_resolution(config_dir)

    return {
        "status": "ok",
        "config_dir": str(config_dir),
        "local_plugin_package": str(local_plugin_package),
        "plugin_version": plugin_version,
        **resolution,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Materialize vendored OpenCode tool dependencies")
    parser.add_argument("--workspace-dir", default=os.getenv("EFP_WORKSPACE_DIR", "/workspace"))
    parser.add_argument("--vendored-dir", default=os.getenv("EFP_OPENCODE_TOOL_DEPS_DIR", "/opt/opencode-tool-deps"))
    parser.add_argument("--opencode-version", default=os.getenv("OPENCODE_VERSION") or None)
    args = parser.parse_args()

    result = ensure_tool_deps(
        workspace_dir=Path(args.workspace_dir),
        vendored_dir=Path(args.vendored_dir),
        opencode_version=args.opencode_version,
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
