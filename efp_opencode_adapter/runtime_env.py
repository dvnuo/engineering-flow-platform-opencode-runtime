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
        proxy_url = _inject_proxy_auth(str(proxy["url"]), proxy.get("username"), proxy.get("password"))
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env[key] = proxy_url
        no_proxy = str(proxy.get("no_proxy") or "127.0.0.1,localhost")
        env["NO_PROXY"] = no_proxy
        env["no_proxy"] = no_proxy
        updated.append("proxy")

    github = cfg.get("github") if isinstance(cfg.get("github"), dict) else {}
    if github:
        token = github.get("api_token")
        if token:
            env["GITHUB_TOKEN"] = str(token)
            env["GITHUB_ACCESS_TOKEN"] = str(token)
        env["GITHUB_API_BASE_URL"] = str(github.get("api_base_url") or "https://api.github.com")
        env["EFP_GITHUB_CONFIG_JSON"] = json.dumps(github, ensure_ascii=False, separators=(",", ":"))
        updated.append("github")

    def _apply_instance(section: str, prefix: str, project_key: str) -> None:
        source = cfg.get(section) if isinstance(cfg.get(section), dict) else {}
        instances = source.get("instances") if isinstance(source.get("instances"), list) else []
        selected = None
        for item in instances:
            if isinstance(item, dict) and item.get("enabled", True) and item.get("url"):
                selected = item
                break
        if not selected:
            return
        env[f"{prefix}_BASE_URL"] = _trim_url(str(selected.get("url")))
        username = selected.get("username") or selected.get("email")
        token = selected.get("token") or selected.get("password")
        if username and token:
            env[f"{prefix}_EMAIL"] = str(username)
            env[f"{prefix}_API_TOKEN"] = str(token)
        elif token:
            env[f"{prefix}_TOKEN"] = str(token)
        if selected.get(project_key):
            env[f"{prefix}_{'PROJECT_KEY' if project_key == 'project' else 'SPACE_KEY'}"] = str(selected.get(project_key))
        env[f"EFP_{prefix}_INSTANCES_JSON"] = json.dumps(instances, ensure_ascii=False, separators=(",", ":"))
        updated.append(section)

    _apply_instance("jira", "JIRA", "project")
    _apply_instance("confluence", "CONFLUENCE", "space")

    git = cfg.get("git") if isinstance(cfg.get("git"), dict) else {}
    if git:
        if git.get("author_name"):
            env["GIT_AUTHOR_NAME"] = str(git["author_name"])
            env["GIT_COMMITTER_NAME"] = str(git["author_name"])
        if git.get("author_email"):
            env["GIT_AUTHOR_EMAIL"] = str(git["author_email"])
            env["GIT_COMMITTER_EMAIL"] = str(git["author_email"])
        updated.append("git")
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
