from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import yaml

from .mobile_cli_config import _read_yaml_mapping
from .path_utils import path_exists
from .settings import Settings
from .tools_config_env import build_cli_env

SECRET_MARKERS = ("TOKEN", "PASSWORD", "SECRET", "API_KEY", "ACCESS", "REFRESH", "AUTHORIZATION")
MANAGED_EXTERNAL_ENV_KEYS = {
    # Full profile Secret blob: scrubbed from the adapter process after boot
    # projection and never allowed to reach the opencode child env.
    "EFP_PROFILE_CONFIG",
    "GITHUB_TOKEN", "GITHUB_ACCESS_TOKEN", "GITHUB_API_BASE_URL", "EFP_GITHUB_CONFIG_JSON",
    "ATLASSIAN_CONFIG",
    "JIRA_BASE_URL", "JIRA_USERNAME", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PASSWORD", "JIRA_TOKEN", "JIRA_PROJECT_KEY", "EFP_JIRA_INSTANCES_JSON",
    "CONFLUENCE_BASE_URL", "CONFLUENCE_USERNAME", "CONFLUENCE_EMAIL", "CONFLUENCE_API_TOKEN", "CONFLUENCE_PASSWORD", "CONFLUENCE_TOKEN", "CONFLUENCE_SPACE_KEY", "EFP_CONFLUENCE_INSTANCES_JSON",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
    "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_HOST", "GH_CONFIG_DIR", "GH_PROMPT_DISABLED", "GH_REPO",
    "GIT_USERNAME", "GIT_PASSWORD", "GIT_ASKPASS", "GIT_TERMINAL_PROMPT", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_EDITOR",
    "JAVA_HOME", "JAVA21_HOME", "JDK21_HOME",
    "MAVEN_HOME", "M2_HOME", "MAVEN_CONFIG", "MAVEN_SETTINGS_PATH",
    "EFP_JENKINS_USERNAME", "EFP_JENKINS_PASSWORD", "JENKINS_USERNAME", "JENKINS_PASSWORD",
    "EFP_CONFIG", "MOBILE_AUTO_STATE_DIR", "MOBILE_AUTO_ARTIFACTS_DIR", "BROWSERSTACK_LOCAL_BINARY",
    "BROWSERSTACK_USERNAME", "BROWSERSTACK_ACCESS_KEY",
    # kubectl reads the managed kubeconfig that `aws-auth eks kubeconfig` writes.
    "KUBECONFIG",
}
_VERSIONED_JAVA_HOME_RE = re.compile(r"^(JAVA|JDK)\d+_HOME$")
_REDACTED_VALUES = {"***redacted***", "[redacted]", "redacted"}
# EFP_-prefixed indexed convention families now feed the shared Go CLIs
# (tools_config_env.build_cli_env). Strip any ambient/stale ones by prefix so a
# previous image's values can't leak past the freshly-built managed env.
MANAGED_EXTERNAL_ENV_PREFIXES = ("EFP_JIRA_", "EFP_CONFLUENCE_", "EFP_JENKINS_")


def _is_managed_external_env_key(key: str) -> bool:
    if key.startswith("AWS_"):
        return True
    if key in MANAGED_EXTERNAL_ENV_KEYS:
        return True
    if key.startswith(MANAGED_EXTERNAL_ENV_PREFIXES):
        return True
    return bool(_VERSIONED_JAVA_HOME_RE.match(key) and key not in {"JAVA21_HOME", "JDK21_HOME"})


def strip_managed_external_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    source = dict(base_env or os.environ)
    return {k: v for k, v in source.items() if not _is_managed_external_env_key(k)}


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


def _first_clean_secret(*values) -> str:
    for value in values:
        cleaned = _clean_secret(value)
        if cleaned:
            return cleaned
    return ""


def _first_text(*values, default: str = "") -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return default


def _path_text(path: Path) -> str:
    return path.as_posix()


def _github_host_from_urls(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        if "://" not in text:
            text = f"https://{text}"
        parts = urlsplit(text)
        host = (parts.hostname or "").strip()
        if not host:
            continue
        if host == "api.github.com":
            return "github.com"
        return host
    return "github.com"


def _is_github_dotcom_like(host: str) -> bool:
    value = str(host or "").strip().lower()
    return value == "github.com" or value.endswith(".ghe.com")


AWS_PROVIDERS = ("adfs-assume", "saml2aws", "assume-role")
AWS_SCALAR_KEYS = (
    "provider",
    "domain",
    "username",
    "password",
    "idp_url",
    "source_profile",
    "default_account",
    "default_region",
    "session_duration_seconds",
    "kubeconfig_path",
)
AWS_ACCOUNT_KEYS = ("name", "account_id", "role", "role_arn", "regions", "profile", "enabled")


def _aws_provider(aws: dict) -> str:
    provider = str(aws.get("provider") or "").strip().lower()
    return provider or "adfs-assume"


def _aws_enabled_accounts(aws: dict) -> list[dict]:
    accounts = aws.get("accounts")
    if not isinstance(accounts, list):
        return []
    out: list[dict] = []
    for item in accounts:
        if not isinstance(item, dict) or item.get("enabled") is False:
            continue
        if not str(item.get("account_id") or item.get("role_arn") or "").strip():
            continue
        out.append(item)
    return out


def _sanitize_aws_node(aws: dict) -> dict:
    """Keep only the keys the aws-auth CLI understands (RootConfig.AWS shape)."""
    node: dict = {"enabled": True}
    for key in AWS_SCALAR_KEYS:
        value = aws.get(key)
        if value is None:
            continue
        if key == "session_duration_seconds":
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if number > 0:
                node[key] = number
            continue
        text = str(value).strip()
        if text:
            node[key] = text
    accounts = aws.get("accounts")
    if isinstance(accounts, list):
        cleaned: list[dict] = []
        for item in accounts:
            if not isinstance(item, dict):
                continue
            entry: dict = {}
            for key in AWS_ACCOUNT_KEYS:
                value = item.get(key)
                if value is None:
                    continue
                if key == "enabled":
                    entry[key] = bool(value)
                elif key == "regions":
                    raw_regions = value if isinstance(value, list) else str(value).split(",")
                    regions = [str(region).strip() for region in raw_regions if str(region).strip()]
                    if regions:
                        entry[key] = regions
                else:
                    text = str(value).strip()
                    if text:
                        entry[key] = text
            if entry.get("name") or entry.get("account_id"):
                cleaned.append(entry)
        node["accounts"] = cleaned
    return node


def _chmod_quietly(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


def _write_aws_cli_config(settings: Settings, aws: dict) -> dict[str, str]:
    """Project the aws node into EFP_CONFIG and pin the AWS/kube file locations.

    aws-auth reads the shared EFP config file for the directory credentials,
    provider, and account matrix, so the node is written there verbatim (only
    known keys). The credentials and role-chaining profiles aws-auth writes,
    and the kubeconfig `aws-auth eks kubeconfig` produces, live under the
    adapter state dir so nothing lands in the browsable workspace. Stale
    artefacts from earlier images and previous sessions are removed at boot.
    """
    aws_dir = settings.adapter_state_dir / "aws"
    aws_dir.mkdir(parents=True, exist_ok=True)
    _chmod_quietly(aws_dir, 0o700)
    config_path = settings.efp_config_path
    existing = _read_yaml_mapping(config_path)
    existing.setdefault("version", 1)
    existing["aws"] = _sanitize_aws_node(aws)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_quietly(config_path.parent, 0o700)
    tmp_path = config_path.with_name(f".{config_path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(yaml.safe_dump(existing, sort_keys=False, allow_unicode=True), encoding="utf-8")
    _chmod_quietly(tmp_path, 0o600)
    tmp_path.replace(config_path)
    _chmod_quietly(config_path, 0o600)
    stale = [aws_dir / "credentials", aws_dir / "config", aws_dir / "config.tmp", aws_dir / "aws-adfs-credential-process.py"]
    stale.extend(aws_dir.glob("adfs-auth*.json"))
    for path in stale:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
    kubeconfig = str(aws.get("kubeconfig_path") or "").strip()
    kubeconfig_path = Path(os.path.expanduser(kubeconfig)) if kubeconfig else settings.adapter_state_dir / "kube" / "config"
    return {
        "EFP_CONFIG": str(config_path),
        "AWS_SHARED_CREDENTIALS_FILE": str(aws_dir / "credentials"),
        "AWS_CONFIG_FILE": str(aws_dir / "config"),
        "KUBECONFIG": str(kubeconfig_path),
    }


def aws_status_from_env(env: dict[str, str]) -> dict[str, object]:
    config_path = env.get("EFP_CONFIG")
    credentials_path = env.get("AWS_SHARED_CREDENTIALS_FILE")
    config_present = bool(config_path and path_exists(Path(config_path)))
    credentials_present = bool(credentials_path and path_exists(Path(credentials_path)))
    kubeconfig_path = env.get("KUBECONFIG")
    return {
        "configured": config_present or credentials_present,
        "config_file_present": config_present,
        "credentials_file_present": credentials_present,
        "config_path": config_path,
        "credentials_path": credentials_path,
        "kubeconfig_path": kubeconfig_path,
        "kubeconfig_present": bool(kubeconfig_path and path_exists(Path(kubeconfig_path))),
    }


@dataclass(frozen=True)
class RuntimeEnvBuildResult:
    env: dict[str, str]
    env_hash: str
    updated_sections: list[str]
    warnings: list[str]


def opencode_xdg_data_home(settings: Settings) -> Path:
    return settings.adapter_state_dir / "xdg-data"


def ensure_opencode_xdg_data_home(settings: Settings) -> Path:
    """Map OpenCode's XDG data path back to the adapter-managed data dir."""
    data_dir = settings.opencode_data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    if data_dir.name == "opencode":
        data_dir.parent.mkdir(parents=True, exist_ok=True)
        return data_dir.parent

    xdg_home = opencode_xdg_data_home(settings)
    xdg_home.mkdir(parents=True, exist_ok=True)
    opencode_path = xdg_home / "opencode"
    desired = data_dir.resolve(strict=False)

    if opencode_path.is_symlink():
        if opencode_path.resolve(strict=False) != desired:
            opencode_path.unlink()
            opencode_path.symlink_to(data_dir, target_is_directory=True)
        return xdg_home

    if opencode_path.exists():
        raise RuntimeError(
            f"OpenCode XDG data path conflict: {opencode_path} already exists and is not managed by the adapter"
        )

    opencode_path.symlink_to(data_dir, target_is_directory=True)
    return xdg_home


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
    xdg_data_home = ensure_opencode_xdg_data_home(settings)
    env: dict[str, str] = {
        "HOME": os.getenv("HOME", "/root"),
        "OPENCODE_CONFIG": str(settings.opencode_config_path),
        "OPENCODE_DATA_DIR": str(settings.opencode_data_dir),
        "XDG_DATA_HOME": str(xdg_data_home),
        # jira/confluence/jenkins reach the Go CLIs through the EFP_-prefixed env
        # convention (see tools_config_env.build_cli_env, merged in below); aws
        # and mobile-auto still resolve config from this shared file.
        "EFP_CONFIG": str(settings.efp_config_path),
        "EFP_RUNTIME_TYPE": "opencode",
        "EFP_WORKSPACE_DIR": str(settings.workspace_dir),
        "EFP_SKILLS_DIR": str(settings.skills_dir),
        "EFP_ADAPTER_STATE_DIR": str(settings.adapter_state_dir),
        "EFP_OPENCODE_URL": settings.opencode_url,
        "MOBILE_AUTO_STATE_DIR": str(settings.mobile_state_dir),
        "MOBILE_AUTO_ARTIFACTS_DIR": str(settings.mobile_artifacts_dir),
        "BROWSERSTACK_LOCAL_BINARY": _path_text(settings.browserstack_local_binary_path),
        "JAVA21_HOME": "/opt/jdks/zulu21",
        "JDK21_HOME": "/opt/jdks/zulu21",
        "JAVA_HOME": "/opt/jdks/zulu21",
        "MAVEN_HOME": "/opt/maven",
        "M2_HOME": "/opt/maven",
        "MAVEN_CONFIG": "/root/.m2",
        "MAVEN_SETTINGS_PATH": "/root/.m2/settings.xml",
    }
    updated: list[str] = ["java_maven"]
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

    github = cfg.get("github") if isinstance(cfg.get("github"), dict) else {}
    github_section_present = isinstance(cfg.get("github"), dict)
    github_enabled = github_section_present and _section_enabled(github)

    # The profile env blob is the sole config source: no ambient process env
    # fallbacks (GH_TOKEN/GITHUB_USERNAME/... leak-back paths were removed).
    if github_section_present and not github_enabled:
        github_token = ""
    else:
        github_token = _first_clean_secret(
            github.get("api_token") if isinstance(github, dict) else None,
            github.get("token") if isinstance(github, dict) else None,
            github.get("access_token") if isinstance(github, dict) else None,
        )
    github_username = _first_text(
        github.get("username") if isinstance(github, dict) else None,
        github.get("login") if isinstance(github, dict) else None,
        (cfg.get("git") or {}).get("username") if isinstance(cfg.get("git"), dict) else None,
        default="x-access-token",
    )
    github_api_base_url = _first_text(
        github.get("api_base_url") if isinstance(github, dict) else None,
        github.get("base_url") if isinstance(github, dict) else None,
        default="https://api.github.com",
    ).rstrip("/")
    github_host = _github_host_from_urls(
        github.get("host") if isinstance(github, dict) else None,
        github.get("web_base_url") if isinstance(github, dict) else None,
        github_api_base_url,
    )
    env["GH_CONFIG_DIR"] = str(settings.adapter_state_dir / "gh")
    env["GH_PROMPT_DISABLED"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = str(settings.adapter_state_dir / "git-askpass.sh")
    env["GIT_CONFIG_GLOBAL"] = str(settings.adapter_state_dir / "gitconfig")
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_EDITOR"] = "true"

    if github_token:
        env["GITHUB_TOKEN"] = github_token
        env["GH_TOKEN"] = github_token
        env["GITHUB_API_BASE_URL"] = github_api_base_url
        env["GH_HOST"] = github_host
        env["GIT_USERNAME"] = github_username
        env["GIT_PASSWORD"] = github_token
        if not _is_github_dotcom_like(github_host):
            env["GH_ENTERPRISE_TOKEN"] = github_token
            env["GITHUB_ENTERPRISE_TOKEN"] = github_token
        updated.append("github")
    elif github_enabled:
        warnings.append("github enabled but no token provided")

    # jira/confluence/jenkins are projected into the EFP_-prefixed indexed env
    # convention consumed by the shared Go CLIs (byte-identical to native). This
    # replaces the former flat JIRA_*/CONFLUENCE_* exports and the config.yaml
    # file write; the CLIs decode EFP_<PATH> vars directly.
    cli_env = build_cli_env(cfg)
    if cli_env:
        env.update(cli_env)
        for section in ("jira", "confluence", "jenkins"):
            if any(key.startswith(f"EFP_{section.upper()}_") for key in cli_env):
                updated.append(section)

    aws = cfg.get("aws") if isinstance(cfg.get("aws"), dict) else {}
    aws_section_present = isinstance(cfg.get("aws"), dict)
    aws_enabled = aws_section_present and _section_enabled(aws)
    if aws_enabled:
        provider = _aws_provider(aws)
        aws_domain = _first_text(aws.get("domain"))
        aws_username = _first_text(aws.get("username"))
        aws_password = _first_clean_secret(aws.get("password"))
        if provider not in AWS_PROVIDERS:
            warnings.append(f"aws provider {provider!r} is not supported; use adfs-assume, saml2aws, or assume-role")
        elif provider != "assume-role" and not (aws_domain and aws_username and aws_password):
            warnings.append("aws enabled but domain, username, and password are required")
        elif provider == "assume-role" and not _aws_enabled_accounts(aws):
            warnings.append("aws enabled with provider assume-role but no accounts are configured")
        else:
            # The node (credentials, provider, account matrix) is written to the
            # shared EFP config file that aws-auth reads; credentials, role
            # profiles and kubeconfig stay under the adapter state dir.
            env.update(_write_aws_cli_config(settings, aws))
            updated.append("aws")

    mobile = cfg.get("mobile-auto") if isinstance(cfg.get("mobile-auto"), dict) else {}
    mobile_section_present = isinstance(cfg.get("mobile-auto"), dict)
    mobile_enabled = mobile_section_present and _section_enabled(mobile)
    if mobile_enabled:
        browserstack = mobile.get("browserstack") if isinstance(mobile.get("browserstack"), dict) else {}
        username = _first_text(browserstack.get("username"))
        access_key = _first_clean_secret(browserstack.get("access_key"))
        username_env = _first_text(browserstack.get("username_env"), default="BROWSERSTACK_USERNAME")
        access_key_env = _first_text(browserstack.get("access_key_env"), default="BROWSERSTACK_ACCESS_KEY")
        if username and username_env:
            env[username_env] = username
            env["BROWSERSTACK_USERNAME"] = username
        if access_key and access_key_env:
            env[access_key_env] = access_key
            env["BROWSERSTACK_ACCESS_KEY"] = access_key
        updated.append("mobile-auto")

    git = cfg.get("git") if isinstance(cfg.get("git"), dict) else {}
    git_user = git.get("user") if isinstance(git.get("user"), dict) else {}
    author_name = git.get("author_name") or git_user.get("name") or github_username
    author_email = git.get("author_email") or git_user.get("email")
    git_env_written = False
    if author_name:
        env["GIT_AUTHOR_NAME"] = str(author_name)
        env["GIT_COMMITTER_NAME"] = str(author_name)
        git_env_written = True
    if author_email:
        env["GIT_AUTHOR_EMAIL"] = str(author_email)
        env["GIT_COMMITTER_EMAIL"] = str(author_email)
        git_env_written = True
    if git_env_written:
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
    if not path_exists(path):
        return data
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return data
    for line in lines:
        line = line.strip()
        if not line.startswith("export ") or "=" not in line:
            continue
        key, value = line[len("export "):].split("=", 1)
        data[key] = shlex.split(value)[0] if value else ""
    return data


def _redact_url_userinfo(value: str) -> str:
    return re.sub(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s?#@]+@", r"\1[redacted]@", str(value))


def redact_env_for_status(env: dict[str, str]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in env.items():
        if any(marker in key.upper() for marker in SECRET_MARKERS):
            out[key] = bool(value)
        elif key in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"}:
            out[key] = _redact_url_userinfo(value)
        else:
            out[key] = value
    return out
