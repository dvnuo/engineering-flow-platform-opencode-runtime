from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .path_utils import path_exists
from .settings import Settings

REDACTED = "***REDACTED***"
SECRET_KEYS = ("api_key", "token", "secret", "password", "authorization", "credential", "access", "refresh", "oauth", "access_token", "refresh_token")


PUBLIC_REDACTED = "[redacted]"


def contains_secret_marker(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in SECRET_KEYS)


def sanitize_public_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if contains_secret_marker(key):
                continue
            if key == "required" and isinstance(item, list):
                out[key] = [x for x in item if not (isinstance(x, str) and contains_secret_marker(x))]
            else:
                out[key] = sanitize_public_secrets(item)
        return out
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, str) and contains_secret_marker(item):
                continue
            result.append(sanitize_public_secrets(item))
        return result
    if isinstance(value, str):
        return PUBLIC_REDACTED if contains_secret_marker(value) else value
    return value


@dataclass(frozen=True)
class ProfileOverlay:
    """Boot record of the last env-payload projection.

    Written unconditionally at every boot so a stale file can never report an
    old revision. Config activation is restart-only; there is no apply
    lifecycle (pending_restart/applied/health) to track anymore.
    """

    runtime_profile_id: str | None
    revision: int | None
    config: dict[str, Any]
    applied_at: str
    generated_config_hash: str
    warnings: list[str] = field(default_factory=list)
    updated_sections: list[str] = field(default_factory=list)
    env_hash: str | None = None
    env_path: str | None = None
    git_auth_configured: bool = False
    gh_host: str | None = None
    gh_config_dir: str | None = None
    git_askpass_path: str | None = None
    gitconfig_path: str | None = None
    atlassian_cli_configured: bool = False
    atlassian_config_path: str | None = None
    atlassian_jira_instances: int = 0
    atlassian_confluence_instances: int = 0
    mobile_cli_configured: bool = False
    mobile_config_path: str | None = None
    mobile_status: dict[str, Any] = field(default_factory=dict)
    aws_configured: bool = False


def redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            lowered = key.lower()
            if any(marker in lowered for marker in SECRET_KEYS):
                redacted[key] = REDACTED
            else:
                redacted[key] = redact_secrets(item)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value


def strip_secret_fields(value: Any) -> Any:
    return sanitize_public_secrets(value)


def sanitize_profile_config_for_storage(config: dict[str, Any]) -> dict[str, Any]:
    clean = redact_secrets(config)
    return clean if isinstance(clean, dict) else {}


class ProfileOverlayStore:
    def __init__(self, settings: Settings):
        self.path = settings.adapter_state_dir / "runtime-profile-overlay.json"

    def load(self) -> ProfileOverlay | None:
        if not path_exists(self.path):
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        config = payload.get("config")
        if not isinstance(config, dict):
            config = {}
        return ProfileOverlay(
            runtime_profile_id=payload.get("runtime_profile_id"),
            revision=payload.get("revision"),
            config=sanitize_profile_config_for_storage(config),
            applied_at=str(payload.get("applied_at") or ""),
            generated_config_hash=str(payload.get("generated_config_hash") or ""),
            warnings=[str(x) for x in payload.get("warnings", []) if isinstance(x, str)],
            updated_sections=[str(x) for x in payload.get("updated_sections", []) if isinstance(x, str)],
            env_hash=(str(payload.get("env_hash")) if payload.get("env_hash") is not None else None),
            env_path=(str(payload.get("env_path")) if payload.get("env_path") is not None else None),
            git_auth_configured=bool(payload.get("git_auth_configured", False)),
            gh_host=(str(payload.get("gh_host")) if payload.get("gh_host") is not None else None),
            gh_config_dir=(str(payload.get("gh_config_dir")) if payload.get("gh_config_dir") is not None else None),
            git_askpass_path=(str(payload.get("git_askpass_path")) if payload.get("git_askpass_path") is not None else None),
            gitconfig_path=(str(payload.get("gitconfig_path")) if payload.get("gitconfig_path") is not None else None),
            atlassian_cli_configured=bool(payload.get("atlassian_cli_configured", False)),
            atlassian_config_path=(str(payload.get("atlassian_config_path")) if payload.get("atlassian_config_path") is not None else None),
            atlassian_jira_instances=int(payload.get("atlassian_jira_instances", 0) or 0),
            atlassian_confluence_instances=int(payload.get("atlassian_confluence_instances", 0) or 0),
            mobile_cli_configured=bool(payload.get("mobile_cli_configured", False)),
            mobile_config_path=(str(payload.get("mobile_config_path")) if payload.get("mobile_config_path") is not None else None),
            mobile_status=payload.get("mobile_status") if isinstance(payload.get("mobile_status"), dict) else {},
            aws_configured=bool(payload.get("aws_configured", False)),
        )

    def save(self, overlay: ProfileOverlay) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = Path(f"{self.path}.tmp")
        payload = asdict(overlay)
        payload["config"] = sanitize_profile_config_for_storage(overlay.config)
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(self.path)


def build_profile_status_payload(settings: Settings) -> dict[str, Any]:
    overlay = ProfileOverlayStore(settings).load()
    if not overlay:
        return {"engine": "opencode", "source": "boot", "runtime_profile_id": None, "revision": None, "warnings": [], "updated_sections": []}
    return {
        "engine": "opencode",
        "source": "boot",
        "runtime_profile_id": overlay.runtime_profile_id,
        "revision": overlay.revision,
        "updated_sections": overlay.updated_sections,
        "config_hash": overlay.generated_config_hash,
        "warnings": overlay.warnings,
        "applied_at": overlay.applied_at,
        "env_hash": overlay.env_hash,
        "env_path": overlay.env_path,
        "git_auth_configured": overlay.git_auth_configured,
        "gh_host": overlay.gh_host,
        "gh_config_dir": overlay.gh_config_dir,
        "git_askpass_path": overlay.git_askpass_path,
        "gitconfig_path": overlay.gitconfig_path,
        "atlassian_cli_configured": overlay.atlassian_cli_configured,
        "atlassian_config_path": overlay.atlassian_config_path,
        "atlassian_jira_instances": overlay.atlassian_jira_instances,
        "atlassian_confluence_instances": overlay.atlassian_confluence_instances,
        "mobile_cli_configured": overlay.mobile_cli_configured,
        "mobile_config_path": overlay.mobile_config_path,
        "mobile_status": overlay.mobile_status,
        "aws_configured": overlay.aws_configured,
    }
