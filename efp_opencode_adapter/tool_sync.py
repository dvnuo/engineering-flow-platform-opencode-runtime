from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_empty_index(state_dir: Path, warning_message: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "generated_at": _utc_now(),
        "tools": [],
        "warnings": [warning_message],
    }
    (state_dir / "tools-index.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    warnings.warn(warning_message, UserWarning)
    return payload


def _redact_secrets(text: str, env: dict[str, str]) -> str:
    redacted = text
    for key, value in env.items():
        if not value:
            continue
        upper = key.upper()
        if any(token in upper for token in ("TOKEN", "PASSWORD", "SECRET", "API_KEY", "ACCESS_KEY")):
            redacted = redacted.replace(value, "***REDACTED***")
    return redacted


def _generator_prefers_output_dir(generator: Path) -> bool:
    try:
        text = generator.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False
    return "--output-dir" in text and "--opencode-tools-dir" not in text


def _build_tools_index_from_registry(tools_dir_path: Path) -> dict[str, Any] | None:
    python_dir = tools_dir_path / "python"
    inserted: str | None = None
    if python_dir.exists():
        inserted = str(python_dir)
        sys.path.insert(0, inserted)
    try:
        import importlib

        module = importlib.import_module("efp_tools.registry")
        load_registry = getattr(module, "load_registry")
        reg = load_registry(tools_dir_path)
        descriptors = reg.list_descriptors(runtime_type="opencode", enabled_only=True, model_facing_only=True)
        items = []
        for d in descriptors:
            items.append({"capability_id": d.tool_id, "tool_id": d.tool_id, "name": d.opencode_name, "opencode_name": d.opencode_name, "legacy_name": d.name, "description": d.description, "domain": d.domain, "type": d.type, "runtime_compat": d.runtime_compat, "policy_tags": d.policy_tags, "requires_identity_binding": d.requires_identity_binding, "mutation": d.mutation, "risk_level": d.risk_level, "input_schema": d.input_schema, "output_schema": d.output_schema, "enabled": d.enabled, "source_ref": "tools_repo"})
        return {"generated_at": _utc_now(), "tools": items, "source": "efp_tools.registry"}
    except Exception:
        return None
    finally:
        if inserted and sys.path and sys.path[0] == inserted:
            sys.path.pop(0)
        sys.modules.pop("efp_tools.registry", None)
        sys.modules.pop("efp_tools", None)


def sync_tools(
    tools_dir: str | Path,
    opencode_tools_dir: str | Path,
    state_dir: str | Path,
) -> dict[str, Any]:
    tools_dir_path = Path(tools_dir)
    opencode_tools_dir_path = Path(opencode_tools_dir)
    state_dir_path = Path(state_dir)

    opencode_tools_dir_path.mkdir(parents=True, exist_ok=True)
    state_dir_path.mkdir(parents=True, exist_ok=True)

    manifest = tools_dir_path / "manifest.yaml"
    if not tools_dir_path.exists():
        return _write_empty_index(state_dir_path, f"tools directory does not exist: {tools_dir_path}")
    if not manifest.exists():
        return _write_empty_index(state_dir_path, f"tools manifest not found: {manifest}")

    generator = tools_dir_path / "adapters" / "opencode" / "generate_tools.py"
    if not generator.exists():
        raise RuntimeError(f"tools generator missing for manifest-backed repo: {generator}")

    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{tools_dir_path / 'python'}:{existing_pythonpath}" if existing_pythonpath else str(tools_dir_path / "python")
    env["EFP_TOOLS_DIR"] = str(tools_dir_path)

    args = [sys.executable, str(generator), "--tools-dir", str(tools_dir_path)]
    if _generator_prefers_output_dir(generator):
        args.extend(["--output-dir", str(opencode_tools_dir_path)])
    else:
        args.extend(["--opencode-tools-dir", str(opencode_tools_dir_path), "--state-dir", str(state_dir_path)])

    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    if result.returncode != 0:
        combined = f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
        redacted = _redact_secrets(combined, env)
        snippet = redacted[:4000]
        raise RuntimeError(
            f"tools generator failed with exit code {result.returncode}. output (truncated):\n{snippet}"
        )

    index_path = state_dir_path / "tools-index.json"
    registry_index = _build_tools_index_from_registry(tools_dir_path)
    if registry_index is not None:
        index_path.write_text(json.dumps(registry_index, ensure_ascii=False, indent=2), encoding="utf-8")
        return registry_index
    if not index_path.exists():
        raise RuntimeError(f"tools generator succeeded but missing index file: {index_path}")

    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"tools index is not valid JSON: {index_path}") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("tools"), list):
        raise RuntimeError(f"tools index missing required tools list: {index_path}")

    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync EFP tools into OpenCode wrappers")
    parser.add_argument("--tools-dir", required=True)
    parser.add_argument("--opencode-tools-dir", required=True)
    parser.add_argument("--state-dir", required=True)
    args = parser.parse_args()

    payload = sync_tools(args.tools_dir, args.opencode_tools_dir, args.state_dir)
    print(
        json.dumps(
            {
                "status": "ok",
                "tools": len(payload.get("tools", [])),
                "index_path": str(Path(args.state_dir) / "tools-index.json"),
            }
        )
    )


if __name__ == "__main__":
    main()
