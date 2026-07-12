"""Project runtime-profile jira/confluence/jenkins into EFP_-prefixed env vars.

This is a byte-for-byte port of the tools-config builder + flattener from the
native repo (engineering-flow-platform/src/external_cli/profile_config.py). The
Go CLIs (engineering-flow-platform-tools) read shared config from EFP_-prefixed
env vars decoded from the RootConfig json tags; opencode must emit the exact
same naming/encoding as native so both runtimes feed the CLIs identically.

Only the pure builder/flattener functions are copied here (no CLI-exec or
metadata bookkeeping); the thin :func:`build_cli_env` wrapper keeps just the
jira/confluence/jenkins sections and flattens them for the managed child env.
"""

from __future__ import annotations

import json
from typing import Any


def build_tools_config_json(effective_config: dict[str, Any]) -> dict[str, Any]:
    """Build the tools config payload (RootConfig-shaped dict) for the Go CLIs.

    The shape matches ``RootConfig`` in engineering-flow-platform-tools
    (internal/config/config.go): top-level keys version/jira/confluence/
    jenkins/aws/visual/mobile-auto. Jira/Confluence/Jenkins sections are
    transformed from the profile shape into the tools instances shape (Jenkins
    is a single flat instance wrapped into a one-element list); the other
    sections are taken from the effective config verbatim. Empty sections are
    omitted.

    The returned dict is flattened by :func:`flatten_config_to_env` into the
    EFP_-prefixed indexed env vars the Go CLIs consume.
    """
    root: dict[str, Any] = {}
    if not isinstance(effective_config, dict):
        return root

    version = effective_config.get("version")
    if isinstance(version, int) and not isinstance(version, bool):
        root["version"] = version

    for product in ("jira", "confluence"):
        section = effective_config.get(product)
        instances = _build_product_instances(section, product=product)
        if not instances:
            continue
        root[product] = {
            "default_instance": _default_instance_name(section, instances),
            "instances": [_tools_instance_config(instance, product=product) for instance in instances],
        }

    jenkins_section = effective_config.get("jenkins")
    jenkins_instances = _build_jenkins_instances(jenkins_section)
    if jenkins_instances:
        root["jenkins"] = {
            "default_instance": _default_instance_name(jenkins_section, jenkins_instances),
            "instances": [_tools_instance_config(instance, product="jenkins") for instance in jenkins_instances],
        }

    for section_name in ("aws", "visual", "mobile-auto"):
        section = effective_config.get(section_name)
        if isinstance(section, dict) and section:
            root[section_name] = json.loads(json.dumps(section))

    return root


def flatten_config_to_env(root: dict[str, Any]) -> dict[str, str]:
    """Flatten a RootConfig-shaped dict into EFP_-prefixed indexed env vars.

    Produces the deterministic naming convention consumed by the Go CLIs: each
    scalar leaf becomes the literal prefix ``EFP_`` plus an UPPERCASED,
    "_"-joined path from the root, with "-" replaced by "_" and list elements
    indexed by their 0-based position. For example
    ``{"jira": {"instances": [{"base_url": "x"}]}}`` yields
    ``{"EFP_JIRA_INSTANCES_0_BASE_URL": "x"}``. The EFP_ prefix keeps these
    names out of other tools' namespaces (AWS_*, JIRA_*, JENKINS_*).

    Scalar encoding: bool -> "true"/"false"; int -> decimal string; str ->
    verbatim. ``None`` and empty strings are omitted entirely (no key emitted),
    matching the "only present values are emitted" contract on the Go side.
    """
    out: dict[str, str] = {}
    _flatten_into(root, (), out)
    return out


def _flatten_into(value: Any, path: tuple[str, ...], out: dict[str, str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            segment = str(key).upper().replace("-", "_")
            _flatten_into(child, path + (segment,), out)
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _flatten_into(child, path + (str(index),), out)
        return
    if value is None:
        return
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, int):
        rendered = str(value)
    else:
        rendered = str(value)
        if rendered == "":
            return
    if not path:
        return
    out["EFP_" + "_".join(path)] = rendered


def _tools_instance_config(instance: dict[str, Any], *, product: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": instance["name"],
        "base_url": instance["base_url"],
        "rest_path": instance["rest_path"],
    }
    if product == "jira":
        out["api_version"] = instance["api_version"]

    auth = instance.get("auth") if isinstance(instance.get("auth"), dict) else {}
    auth_type = str(auth.get("type") or "")
    if auth_type:
        auth_out: dict[str, Any] = {"type": auth_type}
        username = str(auth.get("username") or "")
        if username:
            auth_out["username"] = username
        secret = str(auth.get("secret") or "")
        if secret:
            secret_field = {
                "basic_password": "password",
                "basic_api_key": "api_key",
                "bearer_token": "token",
            }.get(auth_type)
            if secret_field:
                auth_out[secret_field] = secret
        out["auth"] = auth_out
    return out


def _build_product_instances(product_config: Any, *, product: str) -> list[dict[str, Any]]:
    if not isinstance(product_config, dict):
        return []
    if product_config.get("enabled") is False:
        return []
    raw_instances = product_config.get("instances")
    if not isinstance(raw_instances, list):
        return []
    instances: list[dict[str, Any]] = []
    used_names: set[str] = set()
    for index, raw in enumerate(raw_instances, 1):
        if not isinstance(raw, dict) or raw.get("enabled") is False:
            continue
        base_url = _profile_instance_base_url(raw)
        if not base_url:
            continue
        name = _unique_instance_name(str(raw.get("name") or f"{product}-{index}").strip(), used_names, product, index)
        auth = _build_auth(raw)
        if product == "jira":
            api_version = "3" if str(raw.get("api_version") or "").strip() == "3" else "2"
            instance = {
                "name": name,
                "base_url": base_url,
                "api_version": api_version,
                "rest_path": str(raw.get("rest_path") or f"/rest/api/{api_version}"),
                "auth": auth,
            }
        else:
            instance = {
                "name": name,
                "base_url": base_url,
                "rest_path": str(raw.get("rest_path") or "/rest/api"),
                "auth": auth,
            }
        instances.append(instance)
    return instances


def _build_jenkins_instances(section: Any) -> list[dict[str, Any]]:
    """Wrap the flat Jenkins profile section into a single tools instance.

    The Jenkins profile is a single flat ``{enabled, url, username, password}``
    block (not a multi-instance list like Jira/Confluence), so it maps to a
    one-element instances list. Dropped (returns ``[]``) when disabled or when
    no base URL is present, since the Jenkins CLI requires a base URL.
    """
    if not isinstance(section, dict) or section.get("enabled") is False:
        return []
    base_url = _profile_instance_base_url(section)
    if not base_url:
        return []
    auth = _build_auth(section)
    name = str(section.get("name") or "jenkins").strip() or "jenkins"
    return [{"name": name, "base_url": base_url, "rest_path": str(section.get("rest_path") or ""), "auth": auth}]


def _profile_instance_base_url(raw: dict[str, Any]) -> str:
    return _normalize_base_url(raw.get("base_url") or raw.get("baseUrl") or raw.get("url") or raw.get("uri"))


def _unique_instance_name(raw_name: str, used_names: set[str], product: str, index: int) -> str:
    candidate = raw_name or f"{product}-{index}"
    base = candidate
    suffix = 2
    while candidate in used_names:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used_names.add(candidate)
    return candidate


def _default_instance_name(product_config: Any, instances: list[dict[str, Any]]) -> str:
    if not instances:
        return ""
    preferred = ""
    if isinstance(product_config, dict):
        preferred = str(product_config.get("default_instance") or "").strip()
    names = {str(item.get("name") or "") for item in instances}
    return preferred if preferred in names else str(instances[0].get("name") or "")


def _build_auth(raw: dict[str, Any]) -> dict[str, str]:
    username = _string_or_empty(raw.get("username"))
    password = _string_or_empty(raw.get("password"))
    api_key = _string_or_empty(raw.get("api_key") or raw.get("api_token"))
    token = _string_or_empty(raw.get("token") or raw.get("access_token"))
    if username and password:
        return {
            "type": "basic_password",
            "username": username,
            "secret": password,
            "stdin_flag": "--password-stdin",
        }
    if username and (api_key or token):
        return {
            "type": "basic_api_key",
            "username": username,
            "secret": api_key or token,
            "stdin_flag": "--api-key-stdin",
        }
    if token or api_key:
        return {
            "type": "bearer_token",
            "secret": token or api_key,
            "stdin_flag": "--token-stdin",
        }
    return {}


def _normalize_base_url(value: Any) -> str:
    text = _string_or_empty(value)
    return text.rstrip("/")


def _string_or_empty(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def build_cli_env(runtime_config: dict[str, Any] | None) -> dict[str, str]:
    """Build the EFP_-prefixed env vars for the shared Go CLIs (jira/confluence/jenkins).

    Ports the native tools-config projection: build the RootConfig-shaped dict,
    keep ONLY the jira/confluence/jenkins sections (aws stays file-based via
    EFP_CONFIG; mobile-auto keeps its own browserstack env; visual is absent),
    and flatten to the EFP_<PATH> indexed env-var convention the CLIs decode.
    """
    cfg = runtime_config if isinstance(runtime_config, dict) else {}
    root = build_tools_config_json(cfg)
    filtered = {key: root[key] for key in ("jira", "confluence", "jenkins") if key in root}
    return flatten_config_to_env(filtered)
