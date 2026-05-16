from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .settings import Settings

_REDACTED_VALUES = {"***redacted***", "[redacted]", "redacted"}


@dataclass(frozen=True)
class AtlassianCLIConfigResult:
    configured: bool
    path: str
    env: dict[str, str]
    updated_sections: list[str]
    warnings: list[str]
    jira_instances: int
    confluence_instances: int
    redacted_status: dict[str, Any]


def _section_enabled(section: Any) -> bool:
    return isinstance(section, dict) and section.get("enabled") is not False


def _clean_secret(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in _REDACTED_VALUES:
        return ""
    return text


def _first_clean_secret(*values: Any) -> str:
    for value in values:
        cleaned = _clean_secret(value)
        if cleaned:
            return cleaned
    return ""


def _first_text(*values: Any, default: str = "") -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return default


def _normalize_base_url(item: dict[str, Any]) -> str:
    return _first_text(item.get("base_url"), item.get("url")).rstrip("/")


def _normalize_rest_path(value: Any, default: str) -> str:
    text = _first_text(value, default=default)
    if not text.startswith("/"):
        text = f"/{text}"
    return text.rstrip("/") or "/"


def _bool_from_profile(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


def _build_auth(item: dict[str, Any]) -> dict[str, str] | None:
    username = _first_text(item.get("username"), item.get("email"))
    password = _clean_secret(item.get("password"))
    api_key = _first_clean_secret(item.get("token"), item.get("api_token"), item.get("api_key"))
    if username and password:
        return {"type": "basic_password", "username": username, "password": password}
    if username and api_key:
        return {"type": "basic_api_key", "username": username, "api_key": api_key}
    if api_key:
        return {"type": "bearer_token", "token": api_key}
    return None


def _redacted_instance_status(instance: dict[str, Any]) -> dict[str, Any]:
    auth = instance.get("auth") if isinstance(instance.get("auth"), dict) else {}
    return {
        "name": instance.get("name"),
        "base_url_present": bool(instance.get("base_url")),
        "rest_path": instance.get("rest_path"),
        "auth_type": auth.get("type"),
        "username_present": bool(auth.get("username")),
        "password_present": bool(auth.get("password")),
        "api_key_present": bool(auth.get("api_key")),
        "token_present": bool(auth.get("token")),
    }


def _build_section(section_name: str, source: dict[str, Any], warnings: list[str]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    raw_instances = source.get("instances") if isinstance(source.get("instances"), list) else []
    instances: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    for raw in raw_instances:
        if not isinstance(raw, dict) or raw.get("enabled") is False:
            continue
        base_url = _normalize_base_url(raw)
        if not base_url:
            continue
        auth = _build_auth(raw)
        if not auth:
            continue
        name = _first_text(raw.get("name"), default=f"{section_name}-{len(instances) + 1}")
        default_key_name = "default_project" if section_name == "jira" else "default_space"
        default_key_value = _first_text(
            raw.get("project") if section_name == "jira" else raw.get("space"),
            raw.get("project_key") if section_name == "jira" else raw.get("space_key"),
        )
        instance: dict[str, Any] = {
            "name": name,
            "base_url": base_url,
            "rest_path": _normalize_rest_path(raw.get("rest_path"), "/rest/api/2" if section_name == "jira" else "/rest/api"),
            "auth": auth,
            "verify_ssl": _bool_from_profile(raw.get("verify_ssl"), True),
        }
        if section_name == "jira":
            instance["api_version"] = _first_text(raw.get("api_version"), default="2")
        if default_key_value:
            instance[default_key_name] = default_key_value
        instances.append(instance)
        statuses.append(_redacted_instance_status(instance))

    if not instances:
        if source.get("enabled") is True or raw_instances:
            warnings.append(f"{section_name} enabled but no valid instances")
        return None, statuses

    valid_names = {str(item["name"]) for item in instances}
    requested_default = _first_text(source.get("default_instance"))
    default_instance = requested_default if requested_default in valid_names else str(instances[0]["name"])
    return {"default_instance": default_instance, "instances": instances}, statuses


def build_atlassian_cli_config(runtime_config: dict) -> tuple[dict, AtlassianCLIConfigResult]:
    cfg = runtime_config if isinstance(runtime_config, dict) else {}
    warnings: list[str] = []
    config: dict[str, Any] = {"version": 1}
    status: dict[str, Any] = {"configured": False}
    counts: dict[str, int] = {"jira": 0, "confluence": 0}

    for section_name in ("jira", "confluence"):
        source = cfg.get(section_name) if isinstance(cfg.get(section_name), dict) else {}
        if not _section_enabled(source):
            status[section_name] = {"configured": False, "default_instance": None, "instances": []}
            continue
        section_config, instance_statuses = _build_section(section_name, source, warnings)
        if section_config:
            config[section_name] = section_config
            counts[section_name] = len(section_config["instances"])
        status[section_name] = {
            "configured": bool(section_config),
            "default_instance": section_config.get("default_instance") if section_config else None,
            "instances": instance_statuses,
        }

    configured = bool(counts["jira"] or counts["confluence"])
    status["configured"] = configured
    result = AtlassianCLIConfigResult(
        configured=configured,
        path="",
        env={},
        updated_sections=["atlassian"] if configured else [],
        warnings=warnings,
        jira_instances=counts["jira"],
        confluence_instances=counts["confluence"],
        redacted_status=status,
    )
    return config, result


def _chmod_best_effort(path: Path, mode: int, warnings: list[str], warning: str) -> None:
    try:
        path.chmod(mode)
    except OSError:
        warnings.append(warning)


def write_atlassian_cli_config(settings: Settings, runtime_config: dict) -> AtlassianCLIConfigResult:
    config, result = build_atlassian_cli_config(runtime_config)
    path = settings.atlassian_config_path
    warnings = list(result.warnings)
    env = {"ATLASSIAN_CONFIG": str(path)}

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _chmod_best_effort(path.parent, 0o700, warnings, "unable to set atlassian config directory permissions")
    except OSError as exc:
        raise OSError("unable to create atlassian config directory") from exc

    if result.configured:
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _chmod_best_effort(tmp_path, 0o600, warnings, "unable to set atlassian config file permissions")
        tmp_path.replace(path)
        _chmod_best_effort(path, 0o600, warnings, "unable to set atlassian config file permissions")
    elif path.exists():
        try:
            path.unlink()
        except OSError:
            warnings.append("unable to remove stale atlassian config file")

    return replace(result, path=str(path), env=env, warnings=warnings)
