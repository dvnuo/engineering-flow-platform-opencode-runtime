from __future__ import annotations

import argparse
import json
import os
import shutil
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
    dst_node_modules.mkdir(parents=True, exist_ok=True)

    shutil.copytree(src_node_modules, dst_node_modules, dirs_exist_ok=True, symlinks=True)

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

    src_lock = vendored_dir / "package-lock.json"
    dst_lock = config_dir / "package-lock.json"
    if src_lock.exists() and not dst_lock.exists():
        shutil.copy2(src_lock, dst_lock)

    return {
        "status": "ok",
        "config_dir": str(config_dir),
        "local_plugin_package": str(local_plugin_package),
        "plugin_version": plugin_version,
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
