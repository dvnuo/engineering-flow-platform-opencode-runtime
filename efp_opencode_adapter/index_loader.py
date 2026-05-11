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
    return bool({"opencode", "all"} & values)


def normalize_tool_descriptor(raw: dict) -> dict[str, Any] | None:
    cap_id = raw.get("capability_id") or raw.get("tool_id") or raw.get("action_id")
    raw_name = raw.get("name")
    opencode_name = raw.get("opencode_name") or raw_name
    name = opencode_name
    if not cap_id or not name:
        return None
    raw_tags = raw.get("policy_tags")
    tags = raw_tags if isinstance(raw_tags, list) else ([raw_tags] if isinstance(raw_tags, (str, int, float, bool)) else [])
    descriptor = {
        "capability_id": str(cap_id),
        "type": str(raw.get("type") or "adapter_action"),
        "name": str(name),
        "description": str(raw.get("description") or ""),
        "enabled": bool(raw.get("enabled", True)),
        "policy_tags": [str(x) for x in tags if isinstance(x, (str, int, float, bool))],
        "source_ref": str(raw.get("source_ref") or "tools_repo"),
        "opencode_name": str(opencode_name),
        "legacy_name": raw.get("legacy_name") or raw.get("native_name") or raw.get("efp_name") or (raw_name if raw_name and str(raw_name) != str(opencode_name) else None),
        "native_name": raw.get("native_name"),
        "tool_id": raw.get("tool_id"),
    }
    for key in (
        "input_schema",
        "output_schema",
        "requires_identity_binding",
        "domain",
        "runtime_compat",
        "external_system",
        "system_type",
        "mutation",
        "risk_level",
        "permission_default",
        "dry_run_supported",
        "audit_event",
        "side_effects",
        "idempotency_key_fields",
        "governance_reviewed",
        "allow_override",
        "implementation_mode",
        "external_source",
    ):
        if key in raw:
            descriptor[key] = raw[key]
    for key, value in raw.items():
        if key not in descriptor:
            descriptor[key] = value
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
    """External tools removed from runtime contract."""
    return {"tools": []}
