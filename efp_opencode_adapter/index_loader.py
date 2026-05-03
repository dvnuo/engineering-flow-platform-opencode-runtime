from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from .settings import Settings


def read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def read_yaml_file(path: Path) -> dict[str, Any] | list[Any] | None:
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, (dict, list)) else None


def load_skills_index(settings: Settings) -> dict[str, Any]:
    return read_json_file(settings.adapter_state_dir / "skills-index.json") or {"skills": []}


def is_opencode_compatible_tool(tool: dict[str, Any]) -> bool:
    compat = tool.get("runtime_compat")
    if compat is None:
        return True
    if isinstance(compat, str):
        values = {compat.lower()}
    elif isinstance(compat, list):
        values = {str(x).lower() for x in compat}
    else:
        return True
    return "opencode" in values


def normalize_tool_descriptor(raw: dict) -> dict[str, Any] | None:
    cap_id = raw.get("capability_id") or raw.get("tool_id") or raw.get("action_id")
    name = raw.get("opencode_name") or raw.get("name")
    if not cap_id or not name:
        return None
    descriptor = {
        "capability_id": str(cap_id),
        "type": str(raw.get("type") or "adapter_action"),
        "name": str(name),
        "description": str(raw.get("description") or ""),
        "enabled": bool(raw.get("enabled", True)),
        "policy_tags": [str(x) for x in (raw.get("policy_tags") or []) if isinstance(x, (str, int, float))],
        "source_ref": str(raw.get("source_ref") or "tools_repo"),
    }
    for key in ("input_schema", "output_schema", "requires_identity_binding", "domain", "runtime_compat", "external_system", "system_type"):
        if key in raw:
            descriptor[key] = raw[key]
    return descriptor


def _extract_tool_items(data: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    for key in ("tools", "capabilities", "actions"):
        val = data.get(key)
        if isinstance(val, list):
            return [item for item in val if isinstance(item, dict)]
    return []


def load_tools_index(settings: Settings) -> dict[str, Any]:
    paths = [
        settings.adapter_state_dir / "tools-index.json",
        settings.workspace_dir / ".opencode" / "tools-index.json",
        settings.tools_dir / "tools-index.json",
        settings.tools_dir / "manifest.json",
        settings.tools_dir / "manifest.yaml",
        settings.tools_dir / "manifest.yml",
    ]
    for path in paths:
        data: dict[str, Any] | list[Any] | None
        data = read_yaml_file(path) if path.suffix in {".yaml", ".yml"} else read_json_file(path)
        if data is None:
            continue
        normalized = []
        for item in _extract_tool_items(data):
            tool = normalize_tool_descriptor(item)
            if tool is not None and is_opencode_compatible_tool(tool):
                normalized.append(tool)
        return {"tools": normalized}
    return {"tools": []}
