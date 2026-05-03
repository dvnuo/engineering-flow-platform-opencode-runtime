from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .settings import Settings

REDACTED = "***REDACTED***"
SECRET_KEYS = ("api_key", "token", "secret", "password", "authorization", "credential")


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
            config=config,
            applied_at=str(payload.get("applied_at") or ""),
            generated_config_hash=str(payload.get("generated_config_hash") or ""),
        )

    def save(self, overlay: ProfileOverlay) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = Path(f"{self.path}.tmp")
        tmp_path.write_text(json.dumps(asdict(overlay), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(self.path)
