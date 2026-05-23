from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .index_loader import load_skills_index
from .permission_generator import build_permission
from .settings import Settings

MANAGED_TOP_LEVEL_KEYS = {"permission", "agent", "server", "autoupdate", "share", "provider", "instructions", "_efp_managed"}

ATLASSIAN_INSTRUCTIONS_CONTENT = """# Atlassian CLI

Available Bash commands: `jira`, `confluence`.

- Always use `--json` for command output.
- Inspect Jira capabilities with `jira commands --json`, `jira schema issue.map-csv --json`, and `jira schema issue.bulk-create --json`.
- Inspect Confluence capabilities with `confluence commands --json`; for schema details use `confluence schema <command> --json` for the specific command you intend to run.
- For CSV bulk-create work, never create issues immediately. Inspect the CSV, an example Jira issue, the field catalog, and createmeta. Run `jira issue map-csv`, run `jira issue bulk-create --dry-run`, ask for confirmation, then run `jira issue bulk-create --yes`.
- Use Confluence commands similarly for documentation operations, inspecting schemas and target pages or spaces before writing.
"""

JAVA_MAVEN_INSTRUCTIONS_CONTENT = """# Java and Maven

This runtime includes Azul Zulu JDK 21 and Apache Maven.

Available JDK:
- Azul Zulu JDK 21: /opt/jdks/zulu21

Default:
- JAVA_HOME=/opt/jdks/zulu21
- java, javac, jar, Maven, and direct JDK tools use Zulu JDK 21.

Available commands:
- java
- javac
- jar
- mvn
- jdk
- mvn-jdk

Maven:
- Maven settings.xml is /root/.m2/settings.xml.
- Maven toolchains.xml is /root/.m2/toolchains.xml.
- Prefer mvn -B -ntp for automated agent work.

Usage:
- java -version
- javac -version
- mvn -v
- jdk list
- jdk current
- jdk 21 java -version
- mvn-jdk -v
- mvn-jdk 21 -B -ntp test
- jdeps --version
- jlink --version
- jcmd -h

Rules:
- Work under /workspace or /workspace/repos/.
- Only Zulu JDK 21 is installed in this runtime.
"""


def normalize_opencode_provider_id(provider: str | None) -> str:
    raw = str(provider or "").strip().lower()
    aliases = {
        "github": "github-copilot",
        "copilot": "github-copilot",
        "github_copilot": "github-copilot",
        "github-copilot": "github-copilot",
        "claude": "anthropic",
        "anthropic": "anthropic",
        "openai": "openai",
    }
    return aliases.get(raw, raw)


def model_from_runtime_profile(config: dict) -> str | None:
    llm = config.get("llm") if isinstance(config, dict) else None
    if not isinstance(llm, dict):
        return None
    provider = llm.get("provider")
    model = llm.get("model")
    if isinstance(model, str) and "/" in model:
        prefix, suffix = model.split("/", 1)
        return f"{normalize_opencode_provider_id(prefix)}/{suffix}"
    if provider and model:
        return f"{normalize_opencode_provider_id(provider)}/{model}"
    return None


def _int_or_none(value: object) -> int | None:
    try:
        parsed = int(str(value))
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _clean_string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _copilot_credential_present(llm: dict) -> bool:
    oauth = llm.get("oauth") if isinstance(llm.get("oauth"), dict) else None
    if isinstance(oauth, dict) and (_clean_string(oauth.get("refresh")) or _clean_string(oauth.get("access"))):
        return True
    return bool(_clean_string(llm.get("api_key")))


def provider_config_from_runtime_profile(runtime_config: dict, settings: Settings | None = None) -> dict:
    llm = runtime_config.get("llm") if isinstance(runtime_config.get("llm"), dict) else {}
    provider = normalize_opencode_provider_id(llm.get("provider"))
    if not provider:
        model = llm.get("model")
        if isinstance(model, str) and "/" in model:
            provider = normalize_opencode_provider_id(model.split("/", 1)[0])
    if not provider:
        return {}
    options: dict[str, object] = {}
    base_url = llm.get("base_url") or llm.get("api_base") or llm.get("baseURL") or llm.get("endpoint")
    if provider == "github-copilot" and _copilot_credential_present(llm):
        proxy_base_url = (settings.copilot_proxy_base_url if settings else Settings.from_env().copilot_proxy_base_url).strip().rstrip("/")
        if proxy_base_url:
            options["baseURL"] = proxy_base_url
    elif isinstance(base_url, str) and base_url.strip():
        options["baseURL"] = base_url.strip().rstrip("/")
    timeout_ms = _int_or_none(llm.get("timeout_ms") or llm.get("timeout"))
    if timeout_ms:
        options["timeout"] = timeout_ms
    chunk_timeout_ms = _int_or_none(llm.get("chunk_timeout_ms") or llm.get("chunkTimeout"))
    if chunk_timeout_ms:
        options["chunkTimeout"] = chunk_timeout_ms
    if not options:
        return {}
    return {"provider": {provider: {"options": options}}}


def build_opencode_config(settings: Settings, runtime_config: dict | None = None) -> tuple[dict, str, list[str]]:
    runtime_config = runtime_config if isinstance(runtime_config, dict) else {}
    skills_index = load_skills_index(settings)
    permission = build_permission(runtime_config, skills_index=skills_index, permission_mode=settings.opencode_permission_mode, allow_bash_all=settings.opencode_allow_bash_all)
    generated = {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "share": "disabled",
        "server": {"hostname": "127.0.0.1", "port": 4096},
        "permission": permission,
        "instructions": [
            str(settings.atlassian_instructions_path),
            str(settings.java_maven_instructions_path),
        ],
        "agent": {
            "efp-main": {
                "description": "Portal managed OpenCode primary agent",
                "mode": "primary",
                "steps": 40,
                "permission": {},
            }
        },
    }
    updated = ["permission", "agent", "instructions"]
    model = model_from_runtime_profile(runtime_config)
    if model:
        generated["agent"]["efp-main"]["model"] = model
        updated.append("llm")
    provider_patch = provider_config_from_runtime_profile(runtime_config, settings)
    if provider_patch:
        generated.setdefault("provider", {}).update(provider_patch["provider"])
        updated.append("provider")
    digest_src = json.dumps(generated, sort_keys=True, separators=(",", ":"))
    return generated, hashlib.sha256(digest_src.encode("utf-8")).hexdigest(), updated


def write_opencode_config(settings: Settings, config: dict) -> None:
    write_atlassian_instructions(settings)
    write_java_maven_instructions(settings)
    path = settings.opencode_config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = Path(f"{path}.tmp")
    tmp_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def write_atlassian_instructions(settings: Settings) -> Path:
    path = settings.atlassian_instructions_path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = Path(f"{path}.tmp")
    tmp_path.write_text(ATLASSIAN_INSTRUCTIONS_CONTENT, encoding="utf-8")
    tmp_path.replace(path)
    return path


def write_java_maven_instructions(settings: Settings) -> Path:
    path = settings.java_maven_instructions_path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = Path(f"{path}.tmp")
    tmp_path.write_text(JAVA_MAVEN_INSTRUCTIONS_CONTENT, encoding="utf-8")
    tmp_path.replace(path)
    return path


def _hash_index_payload(payload: dict) -> str:
    src = json.dumps(payload if isinstance(payload, dict) else {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(src.encode("utf-8")).hexdigest()


def merge_with_existing_config(existing: dict | None, generated: dict, *, skills_index: dict) -> dict:
    merged = dict(existing) if isinstance(existing, dict) else {}
    for key, value in generated.items():
        if key in MANAGED_TOP_LEVEL_KEYS:
            merged[key] = value
    merged["_efp_managed"] = {
        "skills_index_hash": _hash_index_payload(skills_index),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return merged
