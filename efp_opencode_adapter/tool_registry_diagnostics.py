from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import aiohttp

from .settings import Settings


def _read_dep(path: Path, dep: str) -> str | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    deps = payload.get("dependencies")
    if not isinstance(deps, dict):
        return None
    value = deps.get(dep)
    return value if isinstance(value, str) else None


def _read_version(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("version")
    return value if isinstance(value, str) else None


def _opencode_binary_version() -> str | dict[str, str] | None:
    try:
        result = subprocess.run(["opencode", "--version"], capture_output=True, text=True, check=False, timeout=5)
    except Exception as exc:
        return {"error_type": type(exc).__name__, "error_repr": repr(exc)}
    if result.returncode != 0:
        return {"error": result.stderr.strip() or result.stdout.strip(), "returncode": str(result.returncode)}
    return result.stdout.strip()


async def _probe_http(settings: Settings, path: str, timeout: int) -> dict[str, Any]:
    url = f"{settings.opencode_url.rstrip('/')}{path}"
    auth = None
    if settings.opencode_server_password:
        auth = aiohttp.BasicAuth(settings.opencode_server_username, settings.opencode_server_password)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, auth=auth, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                text = await resp.text()
                return {
                    "ok": 200 <= resp.status < 300,
                    "status": resp.status,
                    "payload_summary": text[:4000],
                }
    except Exception as exc:
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "error_repr": repr(exc),
        }


async def _run(opencode_url: str | None, workspace_dir: Path, timeout: int) -> int:
    settings = Settings.from_env(opencode_url=opencode_url)
    config_dir = workspace_dir / ".opencode"
    tools_dir = config_dir / "tools"
    node_modules_dir = config_dir / "node_modules"
    runtime_package = Path("/app/runtime/package.json")
    vendored_plugin = Path(os.getenv("EFP_OPENCODE_TOOL_DEPS_DIR", "/opt/opencode-tool-deps")) / "node_modules" / "@opencode-ai" / "plugin" / "package.json"
    workspace_plugin = node_modules_dir / "@opencode-ai" / "plugin" / "package.json"

    payload = {
        "status": "diagnostics",
        "opencode_url": settings.opencode_url,
        "versions": {
            "env_opencode_version": os.getenv("OPENCODE_VERSION"),
            "opencode_binary_version": _opencode_binary_version(),
            "runtime_package_opencode_ai": _read_dep(runtime_package, "opencode-ai"),
            "runtime_package_plugin": _read_dep(runtime_package, "@opencode-ai/plugin"),
            "vendored_plugin_version": _read_version(vendored_plugin),
            "workspace_plugin_version": _read_version(workspace_plugin),
        },
        "paths": {
            "workspace_dir": str(workspace_dir),
            "config_dir": str(config_dir),
            "tools_dir": str(tools_dir),
            "node_modules_dir": str(node_modules_dir),
        },
        "workspace": {
            "node_modules_is_symlink": node_modules_dir.is_symlink(),
            "package_json_exists": (config_dir / "package.json").exists(),
            "package_lock_exists": (config_dir / "package-lock.json").exists(),
            "tool_files": sorted(p.name for p in tools_dir.glob("*") if p.is_file()) if tools_dir.exists() else [],
        },
        "http": {
            "health": await _probe_http(settings, "/global/health", timeout),
            "tool_ids": await _probe_http(settings, "/experimental/tool/ids", timeout),
        },
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--opencode-url", default=None)
    parser.add_argument("--workspace-dir", default=os.getenv("EFP_WORKSPACE_DIR", "/workspace"))
    parser.add_argument("--timeout", type=int, default=10)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.opencode_url, Path(args.workspace_dir), args.timeout)))


if __name__ == "__main__":
    main()
