from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROFILE_CONFIG_ENV = "EFP_PROFILE_CONFIG"
PROFILE_REVISION_ENV = "EFP_PROFILE_REVISION"
PROFILE_ID_ENV = "EFP_PROFILE_ID"


class ProfileEnvError(RuntimeError):
    """Fatal pod misconfiguration: the profile env payload is missing or unparseable."""


def load_profile_env_payload() -> dict[str, Any]:
    """Parse the EFP_PROFILE_CONFIG apply-payload injected from the profile Secret.

    A missing env var means the pod spec is broken (fatal). An empty
    ``"config": {}`` payload is a valid empty profile (efp-profile-none).
    """
    raw = os.environ.get(PROFILE_CONFIG_ENV)
    if raw is None:
        # Wording deliberately avoids sanitize_public_secrets marker words so
        # the error stays readable in /ready and status payloads.
        raise ProfileEnvError(
            f"{PROFILE_CONFIG_ENV} is not set; the pod spec must inject the profile env payload"
        )
    text = raw.strip()
    if not text:
        raise ProfileEnvError(f"{PROFILE_CONFIG_ENV} is empty; expected a JSON object payload")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProfileEnvError(f"{PROFILE_CONFIG_ENV} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProfileEnvError(f"{PROFILE_CONFIG_ENV} must be a JSON object")
    return payload


def profile_env_revision() -> int | str | None:
    raw = (os.getenv(PROFILE_REVISION_ENV) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return raw


def profile_env_profile_id() -> str | None:
    raw = (os.getenv(PROFILE_ID_ENV) or "").strip()
    if not raw or raw.lower() == "none":
        return None
    return raw


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
    efp_config_path: Path
    mobile_state_dir: Path
    mobile_artifacts_dir: Path
    browserstack_local_binary_path: Path
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
    event_replay_limit: int = 500
    event_replay_ttl_seconds: float = 21600.0
    opencode_permission_mode: str = "workspace_full_access"
    opencode_allow_bash_all: bool = True
    copilot_proxy_base_url: str = "http://127.0.0.1:8000/api/internal/copilot"
    copilot_github_api_base_url: str = "https://api.github.com"
    copilot_api_base_url: str = "https://api.enterprise.githubcopilot.com"
    ai_platform_proxy_base_url: str = "http://127.0.0.1:8000/api/internal/ai-platform"

    @classmethod
    def from_env(cls, opencode_url: str | None = None) -> "Settings":
        workspace_dir = Path(os.getenv("EFP_WORKSPACE_DIR", "/workspace"))
        adapter_state_dir = Path(os.getenv("EFP_ADAPTER_STATE_DIR", "/root/.local/share/efp-compat"))
        copilot_proxy_base_url = (os.getenv("EFP_COPILOT_PROXY_BASE_URL") or "http://127.0.0.1:8000/api/internal/copilot").rstrip("/")
        copilot_github_api_base_url = (os.getenv("EFP_COPILOT_GITHUB_API_BASE_URL") or "https://api.github.com").rstrip("/")
        copilot_api_base_url = (os.getenv("EFP_COPILOT_API_BASE_URL") or "https://api.enterprise.githubcopilot.com").rstrip("/")
        ai_platform_proxy_base_url = (os.getenv("EFP_AI_PLATFORM_PROXY_BASE_URL") or "http://127.0.0.1:8000/api/internal/ai-platform").rstrip("/")
        return cls(
            opencode_url=opencode_url or os.getenv("EFP_OPENCODE_URL", "http://127.0.0.1:4096"),
            adapter_state_dir=adapter_state_dir,
            workspace_dir=workspace_dir,
            skills_dir=Path(os.getenv("EFP_SKILLS_DIR", "/app/skills")),
            workspace_repos_dir=Path(os.getenv("EFP_WORKSPACE_REPOS_DIR", str(workspace_dir / "repos"))),
            git_checkout_timeout_seconds=float(os.getenv("EFP_GIT_CHECKOUT_TIMEOUT_SECONDS", "120")),
            opencode_data_dir=Path(os.getenv("OPENCODE_DATA_DIR", "/root/.local/share/opencode")),
            opencode_config_path=Path(os.getenv("OPENCODE_CONFIG", "/workspace/.opencode/opencode.json")),
            efp_config_path=Path(os.getenv("EFP_CONFIG", str(workspace_dir / ".efp" / "config.yaml"))),
            mobile_state_dir=Path(os.getenv("MOBILE_AUTO_STATE_DIR", str(workspace_dir / ".efp" / "mobile-auto" / "runs"))),
            mobile_artifacts_dir=Path(os.getenv("MOBILE_AUTO_ARTIFACTS_DIR", str(workspace_dir / ".efp" / "mobile-auto" / "artifacts"))),
            browserstack_local_binary_path=Path(os.getenv("BROWSERSTACK_LOCAL_BINARY", "/usr/local/bin/BrowserStackLocal")),
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
            event_replay_limit=max(0, int(os.getenv("EFP_EVENT_REPLAY_LIMIT", "500"))),
            event_replay_ttl_seconds=max(1.0, float(os.getenv("EFP_EVENT_REPLAY_TTL_SECONDS", "21600"))),
            opencode_permission_mode=_normalize_opencode_permission_mode(os.getenv("EFP_OPENCODE_PERMISSION_MODE", "workspace_full_access")),
            opencode_allow_bash_all=_env_bool("EFP_OPENCODE_ALLOW_BASH_ALL", True),
            copilot_proxy_base_url=copilot_proxy_base_url,
            copilot_github_api_base_url=copilot_github_api_base_url,
            copilot_api_base_url=copilot_api_base_url,
            ai_platform_proxy_base_url=ai_platform_proxy_base_url,
        )
