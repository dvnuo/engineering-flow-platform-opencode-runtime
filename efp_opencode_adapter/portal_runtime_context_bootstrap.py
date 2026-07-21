"""Boot-time projection of the EFP_PROFILE_CONFIG env payload into runtime assets.

Profile config reaches the pod only through the profile Secret env injection
(EFP_PROFILE_CONFIG / EFP_PROFILE_REVISION / EFP_PROFILE_ID); there is no HTTP
pull and no hot apply. The adapter runs this projection once during startup —
before the managed OpenCode child starts — so readiness can gate on it.
Config changes activate only via a portal-triggered pod restart with a new
Secret.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .atlassian_cli_config import build_atlassian_cli_config
from .opencode_config import build_opencode_config, write_opencode_config
from .opencode_auth import build_opencode_auth_from_runtime_config, clear_opencode_auth_provider
from .copilot_plugin_auth import redact_copilot_secrets, save_or_clear_copilot_plugin_credential
from .profile_store import ProfileOverlay, ProfileOverlayStore, sanitize_profile_config_for_storage
from .runtime_env import aws_status_from_env, build_runtime_env_from_config, write_runtime_env_file
from .runtime_profile_encryption import decrypt_sensitive_fields
from .runtime_profile_projection import project_canonical_for_runtime
from .git_cli_auth import write_git_gh_auth_assets
from .mobile_cli_config import write_mobile_cli_config
from .settings import Settings, load_profile_env_payload


def _sanitize(text: str, api_key: str | None = None) -> str:
    if api_key:
        text = text.replace(api_key, "***REDACTED***")
    return redact_copilot_secrets(text)


@dataclass(frozen=True)
class BootProjectionResult:
    runtime_profile_id: str | None
    revision: int | None
    applied_at: str
    env: dict[str, str]
    env_hash: str
    env_path: str
    config_hash: str
    warnings: list[str] = field(default_factory=list)
    updated_sections: list[str] = field(default_factory=list)
    auth_written: bool = False
    copilot_credential_present: bool = False
    git_auth_configured: bool = False
    atlassian_cli_configured: bool = False
    mobile_cli_configured: bool = False
    aws_configured: bool = False

    def public_summary(self) -> dict[str, Any]:
        return {
            "runtime_profile_id": self.runtime_profile_id,
            "revision": self.revision,
            "applied_at": self.applied_at,
            "env_written": True,
            "env_hash": self.env_hash,
            "config_hash": self.config_hash,
            "warnings": list(self.warnings),
            "updated_sections": list(self.updated_sections),
            "auth_written": self.auth_written,
            "copilot_credential_present": self.copilot_credential_present,
            "git_auth_configured": self.git_auth_configured,
            "atlassian_cli_configured": self.atlassian_cli_configured,
            "mobile_cli_configured": self.mobile_cli_configured,
            "aws_configured": self.aws_configured,
        }


def apply_boot_projection(settings: Settings, payload: dict[str, Any]) -> BootProjectionResult:
    """Project the parsed apply-payload dict into files and the managed child env.

    ``payload`` is the full apply-payload JSON from EFP_PROFILE_CONFIG:
    ``{"runtime_profile_id", "name", "revision", "config": {...}}``. The Secret
    now stores ONE runtime-agnostic canonical ``config`` (no ``runtime_type``
    field); this adapter applies the opencode projection to it below. An empty
    ``config`` is a fully valid profile (run with base config).
    """
    runtime_config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    # Decrypt the ENC:-prefixed sensitive values (api_key/token/password/etc.)
    # that the portal wrote into the profile Secret, using EFP_CONFIG_KEY. This
    # runs BEFORE the per-runtime projection so all downstream building sees
    # plaintext. A no-op when the config has no ENC: values; raises (and lets
    # boot fail loud / readiness stay down) if an ENC: value is present but
    # EFP_CONFIG_KEY is unset or the wrong key.
    runtime_config = decrypt_sensitive_fields(runtime_config)
    # Apply the opencode projection to the canonical config at boot. This
    # transforms the LLM into the opencode form (provider ``github-copilot``,
    # model ``github-copilot/<model>``) that downstream opencode.json / auth
    # building expects — restoring what the portal used to bake into the Secret.
    runtime_config = project_canonical_for_runtime(runtime_config, "opencode")
    runtime_profile_id = payload.get("runtime_profile_id")
    revision = payload.get("revision")

    generated, config_hash, updated_sections = build_opencode_config(settings, runtime_config)
    write_opencode_config(settings, generated)
    copilot_credential_result = save_or_clear_copilot_plugin_credential(settings, runtime_config)
    clear_opencode_auth_provider(settings, "github-copilot")

    auth_build = build_opencode_auth_from_runtime_config(runtime_config)
    auth_written = False
    if auth_build.provider and auth_build.auth_info:
        auth_path = settings.opencode_data_dir / "auth.json"
        auth_path.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if auth_path.exists():
            try:
                existing = json.loads(auth_path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        if not isinstance(existing, dict):
            existing = {}
        existing[auth_build.provider] = auth_build.auth_info
        auth_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        auth_path.chmod(0o600)
        auth_written = True

    warnings: list[str] = []
    if auth_build.warning:
        warnings.append(auth_build.warning)

    # jira/confluence no longer write a config.yaml file; they are projected
    # into the EFP_-prefixed env by build_runtime_env_from_config. This only
    # derives the redacted status / instance counts for reporting.
    _, atlassian_result = build_atlassian_cli_config(runtime_config)
    warnings.extend([item for item in atlassian_result.warnings if item not in warnings])
    if atlassian_result.configured and "atlassian" not in updated_sections:
        updated_sections.append("atlassian")
    env_result = build_runtime_env_from_config(settings, runtime_config)
    warnings.extend([item for item in env_result.warnings if item not in warnings])
    mobile_result = write_mobile_cli_config(settings, runtime_config)
    env_result.env.update(mobile_result.env)
    warnings.extend([item for item in mobile_result.warnings if item not in warnings])
    if mobile_result.configured and "mobile-auto" not in updated_sections:
        updated_sections.append("mobile-auto")
    # opencode.env stays as a write-only boot artifact for interactive shells.
    env_path = write_runtime_env_file(settings, env_result.env)
    git_auth_result = write_git_gh_auth_assets(settings, env_result.env)
    aws_status = aws_status_from_env(env_result.env)
    aws_configured = bool("aws" in env_result.updated_sections and aws_status.get("configured"))
    combined_updated_sections = sorted(set(updated_sections + env_result.updated_sections + atlassian_result.updated_sections + mobile_result.updated_sections))
    applied_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    ProfileOverlayStore(settings).save(ProfileOverlay(
        runtime_profile_id=runtime_profile_id,
        revision=revision,
        config=sanitize_profile_config_for_storage(runtime_config),
        applied_at=applied_at,
        generated_config_hash=config_hash,
        warnings=warnings,
        updated_sections=combined_updated_sections,
        env_hash=env_result.env_hash,
        env_path=str(env_path),
        git_auth_configured=bool(git_auth_result.get("configured")),
        gh_host=git_auth_result.get("host"),
        gh_config_dir=git_auth_result.get("gh_config_dir"),
        git_askpass_path=git_auth_result.get("askpass_path"),
        gitconfig_path=git_auth_result.get("gitconfig_path"),
        atlassian_cli_configured=atlassian_result.configured,
        atlassian_config_path=atlassian_result.path,
        atlassian_jira_instances=atlassian_result.jira_instances,
        atlassian_confluence_instances=atlassian_result.confluence_instances,
        mobile_cli_configured=mobile_result.configured,
        mobile_config_path=mobile_result.path,
        mobile_status=mobile_result.redacted_status,
        aws_configured=aws_configured,
    ))
    return BootProjectionResult(
        runtime_profile_id=runtime_profile_id,
        revision=revision,
        applied_at=applied_at,
        env=env_result.env,
        env_hash=env_result.env_hash,
        env_path=str(env_path),
        config_hash=config_hash,
        warnings=warnings,
        updated_sections=combined_updated_sections,
        auth_written=auth_written,
        copilot_credential_present=copilot_credential_result.credential_present,
        git_auth_configured=bool(git_auth_result.get("configured")),
        atlassian_cli_configured=atlassian_result.configured,
        mobile_cli_configured=mobile_result.configured,
        aws_configured=aws_configured,
    )


def run_boot_projection_from_env(settings: Settings) -> BootProjectionResult:
    """Parse EFP_PROFILE_CONFIG and project it. Missing env is fatal (ProfileEnvError)."""
    return apply_boot_projection(settings, load_profile_env_payload())


def main() -> None:
    # Thin dev/debug wrapper; production projection runs inside adapter startup.
    result = run_boot_projection_from_env(Settings.from_env())
    print(_sanitize(json.dumps(result.public_summary())))


if __name__ == "__main__":
    main()
