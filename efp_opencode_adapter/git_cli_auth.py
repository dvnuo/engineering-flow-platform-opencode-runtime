from __future__ import annotations

import os
import subprocess
from pathlib import Path
from urllib.parse import quote, urlsplit

from .settings import Settings

# The image also registers this hook with `git config --system`, but the
# opencode child is spawned with GIT_CONFIG_NOSYSTEM=1 (runtime_env), so
# /etc/gitconfig is never read and the system registration is inert. The
# effective registration is the one written into the file GIT_CONFIG_GLOBAL
# points at, below.
GC_RECENT_OBJECTS_HOOK_PATH = "/usr/local/bin/opencode-snapshot-recent-objects"
GC_RECENT_OBJECTS_HOOK_ENV = "EFP_GC_RECENT_OBJECTS_HOOK"


def _clean(value: object, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def _gc_recent_objects_hook_command() -> str:
    """Command git gc runs to learn which unreferenced objects to keep.

    Overridable (as a full command) so non-image environments can point at the
    checked-out script; empty when the hook is not installed, so an ordinary
    dev box does not get a gc that fails on every repository.
    """
    override = _clean(os.getenv(GC_RECENT_OBJECTS_HOOK_ENV))
    if override:
        return override.replace("\\", "/")
    if Path(GC_RECENT_OBJECTS_HOOK_PATH).exists():
        return GC_RECENT_OBJECTS_HOOK_PATH
    return ""


def _sanitize_git_config_value(value: object, default: str = "") -> str:
    return _clean(value, default).replace("\r", " ").replace("\n", " ")


def _host_only(value: object) -> str:
    text = _clean(value, "github.com")
    if "://" not in text:
        text = f"https://{text}"
    parts = urlsplit(text)
    return parts.hostname or "github.com"


def _write_text(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def write_git_gh_auth_assets(settings: Settings, env: dict[str, str]) -> dict[str, object]:
    state_dir = settings.adapter_state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        state_dir.chmod(0o700)
    except Exception:
        pass

    token = _clean(
        env.get("GIT_PASSWORD")
        or env.get("GH_TOKEN")
        or env.get("GITHUB_TOKEN")
        or env.get("GH_ENTERPRISE_TOKEN")
        or env.get("GITHUB_ENTERPRISE_TOKEN")
    )
    host = _host_only(env.get("GH_HOST") or "github.com")
    username = _sanitize_git_config_value(env.get("GIT_USERNAME"), "x-access-token")

    gh_config_dir = Path(env.get("GH_CONFIG_DIR") or (state_dir / "gh"))
    gh_config_dir.mkdir(parents=True, exist_ok=True)
    try:
        gh_config_dir.chmod(0o700)
    except Exception:
        pass

    askpass_path = Path(env.get("GIT_ASKPASS") or (state_dir / "git-askpass.sh"))
    gitconfig_path = Path(env.get("GIT_CONFIG_GLOBAL") or (state_dir / "gitconfig"))
    credential_store_path = state_dir / "git-credentials"

    askpass = """#!/usr/bin/env bash
case "$1" in
  *Username*) printf '%s\\n' "${GIT_USERNAME:-x-access-token}" ;;
  *Password*) printf '%s\\n' "${GIT_PASSWORD:-${GH_TOKEN:-${GITHUB_TOKEN:-}}}" ;;
  *) printf '\\n' ;;
esac
"""
    _write_text(askpass_path, askpass, 0o700)

    if token:
        encoded_username = quote(username, safe="")
        encoded_token = quote(token, safe="")
        _write_text(credential_store_path, f"https://{encoded_username}:{encoded_token}@{host}\\n", 0o600)

    author_name = _sanitize_git_config_value(env.get("GIT_AUTHOR_NAME"), username or "EFP Agent")
    author_email = _sanitize_git_config_value(env.get("GIT_AUTHOR_EMAIL"), "efp@example.invalid")
    credential_store_config_path = str(credential_store_path).replace("\\", "/")

    gc_hook_command = _sanitize_git_config_value(_gc_recent_objects_hook_command())
    gc_section = f"\n[gc]\n\trecentObjectsHook = {gc_hook_command}\n" if gc_hook_command else ""

    gitconfig = f"""[user]
\tname = {author_name}
\temail = {author_email}

[credential]
\thelper = store --file={credential_store_config_path}

[safe]
\tdirectory = *

[url "https://{host}/"]
\tinsteadOf = git@{host}:
\tinsteadOf = ssh://git@{host}/
{gc_section}"""
    _write_text(gitconfig_path, gitconfig, 0o600)

    validation = subprocess.run(
        ["git", "config", "--file", str(gitconfig_path), "--list"],
        capture_output=True,
        text=True,
        check=False,
    )
    if validation.returncode != 0:
        stderr_preview = (validation.stderr or "").strip().replace("\n", " ")[:200]
        return {
            "configured": False,
            "credential_configured": False,
            "reason": "gitconfig_invalid",
            "error": stderr_preview,
            "host": host,
            "gh_config_dir": str(gh_config_dir),
            "askpass_path": str(askpass_path),
            "gitconfig_path": str(gitconfig_path),
            "credential_store_path": str(credential_store_path),
        }

    return {
        "configured": True,
        "credential_configured": bool(token),
        "reason": "configured" if token else "missing_token_public_git_ok",
        "host": host,
        "username": username,
        "gh_config_dir": str(gh_config_dir),
        "askpass_path": str(askpass_path),
        "gitconfig_path": str(gitconfig_path),
        "credential_store_path": str(credential_store_path),
        "gc_recent_objects_hook": gc_hook_command,
    }
