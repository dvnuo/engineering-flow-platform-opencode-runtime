from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


@dataclass(frozen=True)
class Settings:
    opencode_url: str
    adapter_state_dir: Path
    workspace_dir: Path
    skills_dir: Path
    tools_dir: Path
    opencode_data_dir: Path
    opencode_config_path: Path
    # Optional build/configured OpenCode package version for observability only.
    # It must never be used as a startup compatibility gate.
    opencode_version: str | None
    ready_timeout_seconds: int
    event_bridge_enabled: bool = True
    event_bridge_initial_backoff_seconds: float = 1.0
    event_bridge_max_backoff_seconds: float = 30.0
    event_bridge_event_preview_chars: int = 2000
    portal_internal_base_url: str | None = None
    portal_agent_id: str | None = None
    portal_internal_token: str | None = None
    portal_metadata_timeout_seconds: float = 5.0
    chat_completion_timeout_seconds: float = 300.0
    chat_completion_poll_seconds: float = 1.0
    opencode_permission_mode: str = "workspace_full_access"
    opencode_allow_bash_all: bool = True

    @classmethod
    def from_env(cls, opencode_url: str | None = None) -> "Settings":
        return cls(
            opencode_url=opencode_url or os.getenv("EFP_OPENCODE_URL", "http://127.0.0.1:4096"),
            adapter_state_dir=Path(os.getenv("EFP_ADAPTER_STATE_DIR", "/root/.local/share/efp-compat")),
            workspace_dir=Path(os.getenv("EFP_WORKSPACE_DIR", "/workspace")),
            skills_dir=Path(os.getenv("EFP_SKILLS_DIR", "/app/skills")),
            tools_dir=Path(os.getenv("EFP_TOOLS_DIR", "/app/tools")),
            opencode_data_dir=Path(os.getenv("OPENCODE_DATA_DIR", "/root/.local/share/opencode")),
            opencode_config_path=Path(os.getenv("OPENCODE_CONFIG", "/workspace/.opencode/opencode.json")),
            opencode_version=(os.getenv("OPENCODE_VERSION") or None),
            ready_timeout_seconds=int(os.getenv("EFP_OPENCODE_READY_TIMEOUT_SECONDS", "60")),
            event_bridge_enabled=_env_bool("EFP_OPENCODE_EVENT_BRIDGE_ENABLED", True),
            event_bridge_initial_backoff_seconds=float(os.getenv("EFP_OPENCODE_EVENT_BRIDGE_INITIAL_BACKOFF_SECONDS", "1.0")),
            event_bridge_max_backoff_seconds=float(os.getenv("EFP_OPENCODE_EVENT_BRIDGE_MAX_BACKOFF_SECONDS", "30.0")),
            event_bridge_event_preview_chars=int(os.getenv("EFP_OPENCODE_EVENT_BRIDGE_EVENT_PREVIEW_CHARS", "2000")),
            portal_internal_base_url=os.getenv("PORTAL_INTERNAL_BASE_URL") or None,
            portal_agent_id=os.getenv("PORTAL_AGENT_ID") or None,
            portal_internal_token=os.getenv("PORTAL_INTERNAL_TOKEN") or None,
            portal_metadata_timeout_seconds=float(os.getenv("PORTAL_METADATA_TIMEOUT_SECONDS", "5")),
            chat_completion_timeout_seconds=float(os.getenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "300")),
            chat_completion_poll_seconds=max(0.1, float(os.getenv("EFP_CHAT_COMPLETION_POLL_SECONDS", "1.0"))),
            opencode_permission_mode=((os.getenv("EFP_OPENCODE_PERMISSION_MODE", "workspace_full_access").strip().lower()) or "workspace_full_access"),
            opencode_allow_bash_all=_env_bool("EFP_OPENCODE_ALLOW_BASH_ALL", True),
        )
