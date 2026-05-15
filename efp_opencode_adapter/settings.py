from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _normalize_opencode_permission_mode(raw: str | None) -> str:
    value = str(raw or "").strip().lower()
    if value in {"profile_policy", "profile-policy", "policy", "restricted"}:
        return "profile_policy"
    return "workspace_full_access"


@dataclass(frozen=True)
class Settings:
    opencode_url: str
    adapter_state_dir: Path
    workspace_dir: Path
    skills_dir: Path
    workspace_repos_dir: Path
    git_checkout_timeout_seconds: float
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
    chat_submit_timeout_seconds: float = 300.0
    chat_auto_continue_enabled: bool = True
    chat_auto_continue_max_turns: int = 3
    chat_auto_continue_prompt: str = "Continue the previous task from exactly where you stopped. Do not repeat completed work. Keep using tools if needed. Stop only when you can provide a final user-visible answer, or clearly state the blocker. Do not answer with a progress-only sentence."
    chat_auto_continue_no_progress_stop: bool = True
    opencode_permission_mode: str = "workspace_full_access"
    opencode_allow_bash_all: bool = True

    @classmethod
    def from_env(cls, opencode_url: str | None = None) -> "Settings":
        return cls(
            opencode_url=opencode_url or os.getenv("EFP_OPENCODE_URL", "http://127.0.0.1:4096"),
            adapter_state_dir=Path(os.getenv("EFP_ADAPTER_STATE_DIR", "/root/.local/share/efp-compat")),
            workspace_dir=Path(os.getenv("EFP_WORKSPACE_DIR", "/workspace")),
            skills_dir=Path(os.getenv("EFP_SKILLS_DIR", "/app/skills")),
            workspace_repos_dir=Path(os.getenv("EFP_WORKSPACE_REPOS_DIR", str(Path(os.getenv("EFP_WORKSPACE_DIR", "/workspace")) / "repos"))),
            git_checkout_timeout_seconds=float(os.getenv("EFP_GIT_CHECKOUT_TIMEOUT_SECONDS", "120")),
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
            chat_submit_timeout_seconds=max(300.0, float(os.getenv("EFP_CHAT_SUBMIT_TIMEOUT_SECONDS", os.getenv("EFP_CHAT_COMPLETION_TIMEOUT_SECONDS", "300")))),
            chat_auto_continue_enabled=_env_bool("EFP_CHAT_AUTO_CONTINUE_ENABLED", True),
            chat_auto_continue_max_turns=max(0, int(os.getenv("EFP_CHAT_AUTO_CONTINUE_MAX_TURNS", "3"))),
            chat_auto_continue_prompt=os.getenv("EFP_CHAT_AUTO_CONTINUE_PROMPT", "Continue the previous task from exactly where you stopped. Do not repeat completed work. Keep using tools if needed. Stop only when you can provide a final user-visible answer, or clearly state the blocker. Do not answer with a progress-only sentence."),
            chat_auto_continue_no_progress_stop=_env_bool("EFP_CHAT_AUTO_CONTINUE_NO_PROGRESS_STOP", True),
            opencode_permission_mode=_normalize_opencode_permission_mode(os.getenv("EFP_OPENCODE_PERMISSION_MODE", "workspace_full_access")),
            opencode_allow_bash_all=_env_bool("EFP_OPENCODE_ALLOW_BASH_ALL", True),
        )
