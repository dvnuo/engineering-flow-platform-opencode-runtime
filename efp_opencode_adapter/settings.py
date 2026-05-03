from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    opencode_url: str
    adapter_state_dir: Path
    workspace_dir: Path
    skills_dir: Path
    tools_dir: Path
    opencode_config_path: Path
    opencode_version: str
    opencode_server_username: str
    opencode_server_password: str | None
    ready_timeout_seconds: int
    portal_internal_base_url: str | None = None
    portal_agent_id: str | None = None
    portal_internal_token: str | None = None
    portal_metadata_timeout_seconds: int = 5

    @classmethod
    def from_env(cls, opencode_url: str | None = None) -> "Settings":
        return cls(
            opencode_url=opencode_url or os.getenv("EFP_OPENCODE_URL", "http://127.0.0.1:4096"),
            adapter_state_dir=Path(os.getenv("EFP_ADAPTER_STATE_DIR", "/home/opencode/.local/share/efp-compat")),
            workspace_dir=Path(os.getenv("EFP_WORKSPACE_DIR", "/workspace")),
            skills_dir=Path(os.getenv("EFP_SKILLS_DIR", "/app/skills")),
            tools_dir=Path(os.getenv("EFP_TOOLS_DIR", "/app/tools")),
            opencode_config_path=Path(os.getenv("OPENCODE_CONFIG", "/workspace/.opencode/opencode.json")),
            opencode_version=os.getenv("OPENCODE_VERSION", "1.14.29"),
            opencode_server_username=os.getenv("OPENCODE_SERVER_USERNAME", "opencode"),
            opencode_server_password=os.getenv("OPENCODE_SERVER_PASSWORD"),
            ready_timeout_seconds=int(os.getenv("EFP_OPENCODE_READY_TIMEOUT_SECONDS", "60")),
            portal_internal_base_url=os.getenv("PORTAL_INTERNAL_BASE_URL") or None,
            portal_agent_id=os.getenv("PORTAL_AGENT_ID") or None,
            portal_internal_token=os.getenv("PORTAL_INTERNAL_TOKEN") or None,
            portal_metadata_timeout_seconds=int(os.getenv("PORTAL_METADATA_TIMEOUT_SECONDS", "5")),
        )
