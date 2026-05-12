from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, urlsplit

from .settings import Settings


def _clean(value: object, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


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
    username = _clean(env.get("GIT_USERNAME"), "x-access-token")

    gh_config_dir = Path(env.get("GH_CONFIG_DIR") or (state_dir / "gh"))
    gh_config_dir.mkdir(parents=True, exist_ok=True)
    try:
        gh_config_dir.chmod(0o700)
    except Exception:
        pass

    askpass_path = Path(env.get("GIT_ASKPASS") or (state_dir / "git-askpass.sh"))
    gitconfig_path = Path(env.get("GIT_CONFIG_GLOBAL") or (state_dir / "gitconfig"))
    credential_store_path = state_dir / "git-credentials"

    if not token:
        return {
            "configured": False,
            "reason": "missing_token",
            "host": host,
            "gh_config_dir": str(gh_config_dir),
            "askpass_path": str(askpass_path),
            "gitconfig_path": str(gitconfig_path),
            "credential_store_path": str(credential_store_path),
        }

    askpass = """#!/usr/bin/env bash
case "$1" in
  *Username*) printf '%s\\n' "${GIT_USERNAME:-x-access-token}" ;;
  *Password*) printf '%s\\n' "${GIT_PASSWORD:-${GH_TOKEN:-${GITHUB_TOKEN:-}}}" ;;
  *) printf '\\n' ;;
esac
"""
    _write_text(askpass_path, askpass, 0o700)

    encoded_username = quote(username, safe="")
    encoded_token = quote(token, safe="")
    _write_text(credential_store_path, f"https://{encoded_username}:{encoded_token}@{host}\\n", 0o600)

    author_name = _clean(env.get("GIT_AUTHOR_NAME"), username)
    author_email = _clean(env.get("GIT_AUTHOR_EMAIL"))
    user_block = f"[user]\\n\\tname = {author_name}\\n"
    if author_email:
        user_block += f"\\temail = {author_email}\\n"

    gitconfig = user_block + f"""
[credential]
	helper = store --file={credential_store_path}

[safe]
	directory = *

[url "https://{host}/"]
	insteadOf = git@{host}:
	insteadOf = ssh://git@{host}/
"""
    _write_text(gitconfig_path, gitconfig, 0o600)

    return {
        "configured": True,
        "host": host,
        "username": username,
        "gh_config_dir": str(gh_config_dir),
        "askpass_path": str(askpass_path),
        "gitconfig_path": str(gitconfig_path),
        "credential_store_path": str(credential_store_path),
    }
