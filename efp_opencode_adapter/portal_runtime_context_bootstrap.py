from __future__ import annotations
import argparse, asyncio, json, os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import aiohttp
from .atlassian_cli_config import write_atlassian_cli_config
from .opencode_config import build_opencode_config, normalize_opencode_provider_id, write_opencode_config
from .opencode_auth import build_opencode_auth_from_runtime_config
from .profile_store import ProfileOverlay, ProfileOverlayStore, sanitize_profile_config_for_storage
from .runtime_env import build_runtime_env_from_config, write_runtime_env_file
from .git_cli_auth import write_git_gh_auth_assets
from .settings import Settings


def _sanitize(text: str, api_key: str | None = None) -> str:
    secrets = [os.getenv("PORTAL_INTERNAL_TOKEN", ""), api_key or ""]
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***REDACTED***")
    return text


async def _run(workspace_dir: Path) -> int:
    base_settings = Settings.from_env()
    settings = replace(base_settings, workspace_dir=workspace_dir, opencode_config_path=workspace_dir / '.opencode' / 'opencode.json')

    base = os.getenv("PORTAL_INTERNAL_BASE_URL")
    agent_id = os.getenv("PORTAL_AGENT_ID")
    require_context = os.getenv("EFP_REQUIRE_PORTAL_RUNTIME_CONTEXT", "false").lower() == "true"
    if not base or not agent_id:
        payload = {"status": "error" if require_context else "skipped", "reason": "portal context env missing"}
        print(json.dumps(payload))
        return 1 if require_context else 0

    token = os.getenv("PORTAL_INTERNAL_TOKEN", "")
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Portal-Internal-Token"] = token
    url = f"{base.rstrip('/')}/api/internal/agents/{agent_id}/runtime-context"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
                r.raise_for_status()
                data = await r.json()
    except Exception as exc:
        status = "error" if require_context else "skipped"
        print(json.dumps({"status": status, "reason": "portal context fetch failed", "error": _sanitize(repr(exc))}))
        return 1 if require_context else 0

    profile = data.get("runtime_profile_context") if isinstance(data, dict) else {}
    runtime_config = (profile or {}).get("config") if isinstance(profile, dict) else {}
    runtime_profile_id = (profile or {}).get("runtime_profile_id") if isinstance(profile, dict) else None
    revision = (profile or {}).get("revision") if isinstance(profile, dict) else None

    generated, config_hash, updated_sections = build_opencode_config(settings, runtime_config if isinstance(runtime_config, dict) else {})
    write_opencode_config(settings, generated)

    llm = runtime_config.get("llm") if isinstance(runtime_config, dict) and isinstance(runtime_config.get("llm"), dict) else {}
    provider = normalize_opencode_provider_id(llm.get("provider"))
    model = llm.get("model")
    if not provider and isinstance(model, str) and "/" in model:
        provider = normalize_opencode_provider_id(model.split("/", 1)[0])

    auth_build = build_opencode_auth_from_runtime_config(runtime_config if isinstance(runtime_config, dict) else {})
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

    atlassian_result = write_atlassian_cli_config(settings, runtime_config if isinstance(runtime_config, dict) else {})
    warnings.extend([item for item in atlassian_result.warnings if item not in warnings])
    if atlassian_result.configured and "atlassian" not in updated_sections:
        updated_sections.append("atlassian")
    env_result = build_runtime_env_from_config(settings, runtime_config if isinstance(runtime_config, dict) else {})
    env_result.env.update(atlassian_result.env)
    warnings.extend([item for item in env_result.warnings if item not in warnings])
    env_path = write_runtime_env_file(settings, env_result.env)
    git_auth_result = write_git_gh_auth_assets(settings, env_result.env)
    combined_updated_sections = sorted(set(updated_sections + env_result.updated_sections + atlassian_result.updated_sections))
    ProfileOverlayStore(settings).save(ProfileOverlay(
        runtime_profile_id=runtime_profile_id,
        revision=revision,
        config=sanitize_profile_config_for_storage(runtime_config if isinstance(runtime_config, dict) else {}),
        applied_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        generated_config_hash=config_hash,
        status="applied",
        pending_restart=False,
        warnings=warnings,
        updated_sections=combined_updated_sections,
        last_apply_error=None,
        applied=True,
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
    ))

    out = {"env_written": True, "env_hash": env_result.env_hash, "auth_written": auth_written, "git_auth_configured": bool(git_auth_result.get("configured")), "gh_host": git_auth_result.get("host"), "atlassian_cli_configured": atlassian_result.configured, "atlassian_config_path": atlassian_result.path, "atlassian_jira_instances": atlassian_result.jira_instances, "atlassian_confluence_instances": atlassian_result.confluence_instances, "atlassian_status": atlassian_result.redacted_status}
    if auth_build.warning:
        out["auth_warning"] = auth_build.warning
    print(json.dumps(out))
    return 0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--workspace-dir", default=os.getenv("EFP_WORKSPACE_DIR", "/workspace"))
    args = p.parse_args()
    raise SystemExit(asyncio.run(_run(Path(args.workspace_dir))))

if __name__ == "__main__":
    main()
