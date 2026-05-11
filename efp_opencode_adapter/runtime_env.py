from __future__ import annotations

import hashlib
import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from .settings import Settings

SECRET_MARKERS = ("TOKEN", "PASSWORD", "SECRET", "API_KEY", "ACCESS", "REFRESH", "AUTHORIZATION")
MANAGED_EXTERNAL_ENV_KEYS = {
    "GITHUB_TOKEN", "GITHUB_ACCESS_TOKEN", "GITHUB_API_BASE_URL", "EFP_GITHUB_CONFIG_JSON",
    "JIRA_BASE_URL", "JIRA_USERNAME", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PASSWORD", "JIRA_TOKEN", "JIRA_PROJECT_KEY", "EFP_JIRA_INSTANCES_JSON",
    "CONFLUENCE_BASE_URL", "CONFLUENCE_USERNAME", "CONFLUENCE_EMAIL", "CONFLUENCE_API_TOKEN", "CONFLUENCE_PASSWORD", "CONFLUENCE_TOKEN", "CONFLUENCE_SPACE_KEY", "EFP_CONFLUENCE_INSTANCES_JSON",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
}
_REDACTED_VALUES = {"***redacted***", "[redacted]", "redacted"}


def strip_managed_external_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    source = dict(base_env or os.environ)
    return {k: v for k, v in source.items() if k not in MANAGED_EXTERNAL_ENV_KEYS}


def _section_enabled(section: dict) -> bool:
    if not isinstance(section, dict):
        return False
    if section.get("enabled") is False:
        return False
    return True


def _clean_secret(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = text.lower()
    if normalized in _REDACTED_VALUES:
        return ""
    return text


@dataclass(frozen=True)
class RuntimeEnvBuildResult:
    env: dict[str, str]
    env_hash: str
    updated_sections: list[str]
    warnings: list[str]


def _trim_url(url: str) -> str:
    return url.rstrip("/")


def _inject_proxy_auth(url: str, username: str | None, password: str | None) -> str:
    if not username and not password:
        return url
    parts = urlsplit(url)
    auth = quote(username or "", safe="")
    if password is not None:
        auth = f"{auth}:{quote(password, safe='')}"
    netloc = f"{auth}@{parts.hostname or ''}"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def build_runtime_env_from_config(settings: Settings, runtime_config: dict | None) -> RuntimeEnvBuildResult:
    cfg = runtime_config if isinstance(runtime_config, dict) else {}
    env: dict[str, str] = {
        "HOME": os.getenv("HOME", "/root"),
        "OPENCODE_CONFIG": str(settings.opencode_config_path),
        "OPENCODE_DATA_DIR": str(settings.opencode_data_dir),
        "EFP_RUNTIME_TYPE": "opencode",
        "EFP_WORKSPACE_DIR": str(settings.workspace_dir),
        "EFP_SKILLS_DIR": str(settings.skills_dir),
        "EFP_TOOLS_DIR": str(settings.tools_dir),
        "EFP_ADAPTER_STATE_DIR": str(settings.adapter_state_dir),
        "EFP_OPENCODE_URL": settings.opencode_url,
    }
    py_path = os.getenv("PYTHONPATH")
    env["PYTHONPATH"] = f"{settings.tools_dir}/python:{py_path}" if py_path else f"{settings.tools_dir}/python"
    updated: list[str] = []
    warnings: list[str] = []

    proxy = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
    if proxy.get("enabled") and proxy.get("url"):
        proxy_url = _inject_proxy_auth(str(proxy["url"]), _clean_secret(proxy.get("username")), _clean_secret(proxy.get("password")))
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env[key] = proxy_url
        no_proxy = str(proxy.get("no_proxy") or "127.0.0.1,localhost")
        env["NO_PROXY"] = no_proxy
        env["no_proxy"] = no_proxy
        updated.append("proxy")

    github = cfg.get("github") if isinstance(cfg.get("github"), dict) else None
    if isinstance(github, dict) and _section_enabled(github):
        token = _clean_secret(github.get("api_token") or github.get("token") or github.get("access_token"))
        if token:
            base_url = str(github.get("api_base_url") or github.get("base_url") or "https://api.github.com").strip().rstrip("/")
            env["GITHUB_TOKEN"] = token
            env["GITHUB_ACCESS_TOKEN"] = token
            env["GITHUB_API_BASE_URL"] = base_url
            normalized_github = {"enabled": True, "api_token": token, "base_url": base_url, "api_base_url": base_url}
            env["EFP_GITHUB_CONFIG_JSON"] = json.dumps(normalized_github, ensure_ascii=False, separators=(",", ":"))
            updated.append("github")

    def _apply_instance(section: str, prefix: str, project_key: str) -> None:
        source = cfg.get(section) if isinstance(cfg.get(section), dict) else {}
        if not _section_enabled(source):
            return
        instances = source.get("instances") if isinstance(source.get("instances"), list) else None
        if not isinstance(instances, list):
            return
        safe_instances = []
        for item in instances:
            if not isinstance(item, dict):
                continue
            if item.get("enabled") is False:
                continue
            raw_url = str(item.get("url") or "").strip()
            if not raw_url:
                continue
            username = str(item.get("username") or item.get("email") or "").strip()
            api_token = _clean_secret(item.get("api_token") or item.get("token"))
            password = _clean_secret(item.get("password"))
            credential_present = bool(api_token or password)
            if not credential_present:
                continue
            safe_item = {
                "enabled": True,
                "url": _trim_url(raw_url),
            }
            if api_token:
                safe_item["token"] = api_token
            if password:
                safe_item["password"] = password
            if item.get("name"):
                safe_item["name"] = str(item.get("name"))
            if username:
                safe_item["username"] = username
            if project_key == "project":
                proj = item.get("project") or item.get("project_key")
                if proj:
                    safe_item["project"] = str(proj)
            else:
                space = item.get("space") or item.get("space_key")
                if space:
                    safe_item["space"] = str(space)
            if password and not username and not api_token:
                continue
            if section == "jira":
                api_version_raw = str(item.get("api_version") or "").strip()
                if api_version_raw in {"2", "3"}:
                    safe_item["api_version"] = api_version_raw
                elif username and password and not api_token:
                    safe_item["api_version"] = "2"
                else:
                    safe_item["api_version"] = "3"
            safe_instances.append(safe_item)
        if not safe_instances:
            warnings.append(f"{section} enabled but no valid instance credential")
            return
        selected = safe_instances[0]
        env[f"{prefix}_BASE_URL"] = selected["url"]
        username = str(selected.get("username") or "").strip()
        api_token = _clean_secret(selected.get("token"))
        password = _clean_secret(selected.get("password"))
        if username and api_token:
            env[f"{prefix}_EMAIL"] = username
            env[f"{prefix}_API_TOKEN"] = api_token
        elif username and password:
            env[f"{prefix}_USERNAME"] = username
            env[f"{prefix}_PASSWORD"] = password
        elif api_token:
            env[f"{prefix}_TOKEN"] = api_token
        else:
            return
        if selected.get(project_key):
            env[f"{prefix}_{'PROJECT_KEY' if project_key == 'project' else 'SPACE_KEY'}"] = str(selected.get(project_key))
        env[f"EFP_{prefix}_INSTANCES_JSON"] = json.dumps(safe_instances, ensure_ascii=False, separators=(",", ":"))
        updated.append(section)

    _apply_instance("jira", "JIRA", "project")
    _apply_instance("confluence", "CONFLUENCE", "space")

    git = cfg.get("git") if isinstance(cfg.get("git"), dict) else {}
    if git:
        git_user = git.get("user") if isinstance(git.get("user"), dict) else {}
        author_name = git.get("author_name") or git_user.get("name")
        author_email = git.get("author_email") or git_user.get("email")
        if author_name:
            env["GIT_AUTHOR_NAME"] = str(author_name)
            env["GIT_COMMITTER_NAME"] = str(author_name)
        if author_email:
            env["GIT_AUTHOR_EMAIL"] = str(author_email)
            env["GIT_COMMITTER_EMAIL"] = str(author_email)
        updated.append("git")
    env.setdefault("OPENCODE_DISABLE_CLAUDE_CODE_PROMPT", "1")
    debug = cfg.get("debug") if isinstance(cfg.get("debug"), dict) else {}
    if debug.get("enabled"):
        env["EFP_DEBUG"] = "1"
    if debug.get("log_level"):
        env["LOG_LEVEL"] = str(debug.get("log_level"))
    if debug:
        updated.append("debug")
    env_hash = hashlib.sha256(json.dumps(env, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return RuntimeEnvBuildResult(env=env, env_hash=env_hash, updated_sections=updated, warnings=warnings)


def write_runtime_env_file(settings: Settings, env: dict[str, str]) -> Path:
    path = settings.adapter_state_dir / "opencode.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in sorted(env.items()))
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return path


def read_runtime_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("export ") or "=" not in line:
            continue
        key, value = line[len("export "):].split("=", 1)
        data[key] = shlex.split(value)[0] if value else ""
    return data


def redact_env_for_status(env: dict[str, str]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in env.items():
        if any(marker in key.upper() for marker in SECRET_MARKERS):
            out[key] = bool(value)
        else:
            out[key] = value
    return out
