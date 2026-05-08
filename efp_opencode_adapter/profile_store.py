from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

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
    runtime_profile_id: str | None
    revision: int | None
    config: dict[str, Any]
    applied_at: str
    generated_config_hash: str
    status: str = "unknown"
    pending_restart: bool = False
    warnings: list[str] = field(default_factory=list)
    updated_sections: list[str] = field(default_factory=list)
    last_apply_error: str | None = None
    applied: bool = False


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
        if not self.path.exists():
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
            status=str(payload.get("status") or "unknown"),
            pending_restart=bool(payload.get("pending_restart", False)),
            warnings=[str(x) for x in payload.get("warnings", []) if isinstance(x, str)],
            updated_sections=[str(x) for x in payload.get("updated_sections", []) if isinstance(x, str)],
            last_apply_error=(str(payload.get("last_apply_error")) if payload.get("last_apply_error") is not None else None),
            applied=bool(payload.get("applied", False)),
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
        return {"engine": "opencode", "status": "unknown", "applied": False, "pending_restart": False, "warnings": [], "updated_sections": [], "restart_required": False}
    return {
        "engine": "opencode",
        "status": overlay.status,
        "runtime_profile_id": overlay.runtime_profile_id,
        "revision": overlay.revision,
        "applied": overlay.applied,
        "pending_restart": overlay.pending_restart,
        "updated_sections": overlay.updated_sections,
        "config_hash": overlay.generated_config_hash,
        "warnings": overlay.warnings,
        "last_apply_error": overlay.last_apply_error,
        "applied_at": overlay.applied_at,
        "restart_required": overlay.pending_restart,
    }
