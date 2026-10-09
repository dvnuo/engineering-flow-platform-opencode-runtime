"""Resolve the Proxy connector into the proxy each consumer gets.

A port of the native runtime's ``src/utils/proxy_plan.py`` (the two runtimes
share no package); keep the two in step.

The Portal's Proxy connector is a list of named proxies, a default, and an
assignment per connector (MULTI_PROXY_CONNECTOR_PLAN.md)::

    proxy:
      enabled: true
      default: corp-a
      proxies:
        - {name: corp-a, url: http://proxy-a.example.test:3128, username: u, password: p, no_proxy: localhost}
        - {name: corp-b, url: http://proxy-b.example.test:3128}
      assignments: {llm: corp-a, pgsql: corp-b, jira: "", aws: ""}

The environment (HTTPS_PROXY, HTTP_PROXY, ALL_PROXY, NO_PROXY) carries the
default proxy, for the tools that only read the environment (aws, kubectl,
gh, git, and opencode itself). Every other consumer asks the plan for its own
choice and gets a *setting* in the vocabulary the Go CLIs already speak:
``""`` to follow the environment, ``none`` to connect directly, or the proxy
URL with its credentials. A profile saved before named proxies existed, with
the flat url/username/password/no_proxy keys, reads as one proxy named
``default``.

Pure functions over the config dict; nothing here touches ``os.environ``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlparse, urlsplit, urlunsplit

# The opencode child talks to this adapter's loopback proxies; NO_PROXY must
# always exempt them, whatever the connector says.
DEFAULT_NO_PROXY = "127.0.0.1,localhost"
LEGACY_ENTRY_NAME = "default"

# What a choice is.
KIND_ENVIRONMENT = "environment"
KIND_NONE = "none"
KIND_PROXY = "proxy"

# Why a choice came out the way it did.
SOURCE_DISABLED = "disabled"
SOURCE_ENVIRONMENT = "environment"
SOURCE_ASSIGNMENT = "assignment"
SOURCE_INSTANCE = "instance"
SOURCE_DEFAULT = "default"
SOURCE_NO_PROXY = "no_proxy"
SOURCE_UNKNOWN = "unknown"

PROXY_URL_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
NO_PROXY_ENV_KEYS = ("NO_PROXY", "no_proxy")
CREDENTIAL_ENV_PREFIX = "EFP_PROXY_"
# What the Model provider connector was assigned, for the adapter's loopback
# proxies (outbound_proxy.py): absent follows the environment, "none" is a
# direct connection, a URL is the proxy; LLM_NO_PROXY_ENV is that proxy's
# own NO_PROXY list.
LLM_PROXY_ENV = "EFP_LLM_PROXY"
LLM_NO_PROXY_ENV = "EFP_LLM_NO_PROXY"

_NONE_WORDS = frozenset({"none", "direct", "off"})
_ENVIRONMENT_WORDS = frozenset({"", "env", "environment"})
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_REDACTED_VALUES = frozenset({"***redacted***", "[redacted]", "redacted"})


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _secret(value: Any) -> str:
    text = _text(value)
    return "" if text.lower() in _REDACTED_VALUES else text


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).lower() in {"1", "true", "yes", "on"}


def proxy_url_with_credentials(url: str, username: str | None, password: str | None) -> str:
    """The proxy URL with the configured credentials as its user info."""
    if not username and not password:
        return url
    parts = urlsplit(url if "://" in url else "http://" + url)
    if not parts.hostname:
        return url
    auth = quote(username or "", safe="")
    if password is not None:
        auth = f"{auth}:{quote(password, safe='')}"
    host = parts.hostname
    if ":" in host:
        host = "[" + host + "]"
    netloc = f"{auth}@{host}"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@dataclass(frozen=True)
class ProxyEntry:
    """One named proxy of the connector."""

    name: str
    url: str
    username: str = ""
    password: str = ""
    no_proxy: str = ""

    def url_with_credentials(self) -> str:
        return proxy_url_with_credentials(self.url, self.username or None, self.password or None)

    def no_proxy_text(self, default: str = DEFAULT_NO_PROXY) -> str:
        """The entry's NO_PROXY, always exempting loopback (the adapter's own proxies)."""
        text = self.no_proxy.strip() or default
        items = [item.strip() for item in re.split(r"[,\s]+", text) if item.strip()]
        lowered = {item.lower() for item in items}
        for required in ("127.0.0.1", "localhost"):
            if required not in lowered:
                items.append(required)
        return ",".join(items)

    def address(self) -> str:
        """host:port without credentials, for a status or a log line."""
        parsed = urlparse(self.url if "://" in self.url else "http://" + self.url)
        host = parsed.hostname or ""
        if ":" in host:
            host = "[" + host + "]"
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme == "https" else 80
        return f"{host}:{port}" if host else ""

    def credential_env_names(self) -> tuple[str, str]:
        """The EFP_PROXY_<NAME>_USERNAME / _PASSWORD variable names."""
        token = re.sub(r"[^A-Za-z0-9]+", "_", self.name).strip("_").upper() or "DEFAULT"
        return (f"{CREDENTIAL_ENV_PREFIX}{token}_USERNAME", f"{CREDENTIAL_ENV_PREFIX}{token}_PASSWORD")


@dataclass(frozen=True)
class ProxyChoice:
    """How one consumer reaches its service."""

    kind: str
    source: str
    entry: ProxyEntry | None = None
    url: str = ""

    @property
    def setting(self) -> str:
        """The value for a tool's proxy field: "", "none", or the proxy URL."""
        if self.kind == KIND_NONE:
            return "none"
        if self.kind == KIND_PROXY:
            return self.entry.url_with_credentials() if self.entry is not None else self.url
        return ""

    def describe(self) -> dict[str, Any]:
        """The choice without credentials."""
        out: dict[str, Any] = {"kind": self.kind, "source": self.source}
        if self.entry is not None:
            out["proxy"] = self.entry.name
            out["address"] = self.entry.address()
        elif self.url:
            parsed = urlparse(self.url if "://" in self.url else "http://" + self.url)
            out["address"] = parsed.hostname or ""
        return out


@dataclass(frozen=True)
class ProxyPlan:
    enabled: bool
    entries: tuple[ProxyEntry, ...] = ()
    default: ProxyEntry | None = None
    assignments: Mapping[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        """Whether the environment gets a proxy at all."""
        return self.enabled and self.default is not None

    def entry(self, name: str) -> ProxyEntry | None:
        wanted = _text(name)
        for item in self.entries:
            if item.name == wanted:
                return item
        return None

    def environment(self, *, default_no_proxy: str = DEFAULT_NO_PROXY) -> dict[str, str]:
        """The variables the opencode child gets: the default proxy, or nothing."""
        if not self.configured:
            return {}
        url = self.default.url_with_credentials()
        env = {key: url for key in PROXY_URL_ENV_KEYS}
        no_proxy = self.default.no_proxy_text(default_no_proxy)
        for key in NO_PROXY_ENV_KEYS:
            env[key] = no_proxy
        return env

    def credential_environment(self) -> dict[str, str]:
        """EFP_PROXY_<NAME>_USERNAME/_PASSWORD for every proxy with credentials.

        mobile-auto names its proxy credentials by environment variable
        (``proxy_user_env`` / ``proxy_pass_env``), so they must exist in the
        environment of the CLI; nothing else reads them.
        """
        if not self.enabled:
            return {}
        env: dict[str, str] = {}
        for item in self.entries:
            if not (item.username or item.password):
                continue
            user_key, pass_key = item.credential_env_names()
            env[user_key] = item.username
            env[pass_key] = item.password
        return env

    def choice(self, connector: str, *, host: str | None = None, instance_setting: str | None = None) -> ProxyChoice:
        """The proxy for ``connector`` (a Portal connector type: llm, jira, pgsql, ...).

        ``instance_setting`` is the row's own proxy field when the tool has one:
        ``none`` forces a direct connection, a proxy name picks that proxy, a
        URL is used as it is. Otherwise the connector's assignment decides:
        empty follows the environment, ``none`` connects directly, a name picks
        that proxy. A proxy whose ``no_proxy`` exempts ``host`` becomes a direct
        connection, and the default proxy is answered as "the environment",
        which already is that proxy and lets the tool apply NO_PROXY per host
        the way it always did.
        """
        # The row's own setting is explicit and stands on its own: none and a
        # URL apply whatever the connector's switch says; a name needs the
        # connector's list, so it only resolves while the connector is on.
        own = _text(instance_setting)
        if own:
            if own.lower() in _NONE_WORDS:
                return ProxyChoice(KIND_NONE, SOURCE_INSTANCE)
            entry = self.entry(own) if self.enabled else None
            if entry is not None:
                return self._entry_choice(entry, host, SOURCE_INSTANCE)
            if _looks_like_url(own):
                return ProxyChoice(KIND_PROXY, SOURCE_INSTANCE, url=own)
        if not self.enabled:
            return ProxyChoice(KIND_ENVIRONMENT, SOURCE_DISABLED)
        assigned = _text(self.assignments.get(connector))
        if assigned.lower() in _ENVIRONMENT_WORDS:
            return ProxyChoice(KIND_ENVIRONMENT, SOURCE_ENVIRONMENT)
        if assigned.lower() in _NONE_WORDS:
            return ProxyChoice(KIND_NONE, SOURCE_ASSIGNMENT)
        entry = self.entry(assigned)
        if entry is None:
            return ProxyChoice(KIND_ENVIRONMENT, SOURCE_UNKNOWN)
        return self._entry_choice(entry, host, SOURCE_ASSIGNMENT)

    def _entry_choice(self, entry: ProxyEntry, host: str | None, source: str) -> ProxyChoice:
        if host and no_proxy_exempts(host, entry.no_proxy_text()):
            return ProxyChoice(KIND_NONE, SOURCE_NO_PROXY, entry=entry)
        if source == SOURCE_ASSIGNMENT and self.default is not None and entry.name == self.default.name:
            return ProxyChoice(KIND_ENVIRONMENT, SOURCE_DEFAULT, entry=entry)
        return ProxyChoice(KIND_PROXY, source, entry=entry)

    def summary(self) -> dict[str, Any]:
        """The plan without credentials, for the status endpoint and the logs."""
        return {
            "enabled": self.enabled,
            "default": self.default.name if self.default is not None else None,
            "proxies": [{"name": item.name, "address": item.address()} for item in self.entries],
            "assignments": {key: value for key, value in self.assignments.items()},
            "warnings": list(self.warnings),
        }


def _looks_like_url(value: str) -> bool:
    text = _text(value)
    if "://" in text:
        return True
    host, sep, port = text.rpartition(":")
    return bool(sep) and host != "" and port.isdigit()


def normalize_proxy_section(raw: Any) -> dict[str, Any]:
    """The proxy section in its named-proxies shape, whatever shape it came in.

    Returns ``{enabled, default, proxies: [...], assignments: {...}}`` with
    every entry carrying name/url/username/password/no_proxy. The flat legacy
    keys become one proxy named ``default`` when no list is present.
    """
    section = raw if isinstance(raw, Mapping) else {}
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    raw_entries = section.get("proxies")
    if isinstance(raw_entries, list):
        for index, item in enumerate(raw_entries, 1):
            if not isinstance(item, Mapping):
                continue
            url = _text(item.get("url"))
            if not url:
                continue
            name = _text(item.get("name")) or f"proxy-{index}"
            if name in seen:
                continue
            seen.add(name)
            entries.append(
                {
                    "name": name,
                    "url": url,
                    "username": _secret(item.get("username")),
                    "password": _secret(item.get("password")),
                    "no_proxy": _no_proxy_text(item),
                }
            )
    if not entries and _text(section.get("url")):
        entries.append(
            {
                "name": LEGACY_ENTRY_NAME,
                "url": _text(section.get("url")),
                "username": _secret(section.get("username")),
                "password": _secret(section.get("password")),
                "no_proxy": _no_proxy_text(section),
            }
        )
    names = [item["name"] for item in entries]
    default = _text(section.get("default"))
    if default not in names:
        default = names[0] if names else ""
    assignments: dict[str, str] = {}
    raw_assignments = section.get("assignments")
    if isinstance(raw_assignments, Mapping):
        for key, value in raw_assignments.items():
            connector = _text(key)
            if connector:
                assignments[connector] = _text(value)
    return {
        "enabled": _flag(section.get("enabled")),
        "default": default,
        "proxies": entries,
        "assignments": assignments,
    }


def _no_proxy_text(item: Mapping[str, Any]) -> str:
    value = item.get("no_proxy")
    if value is None or _text(value) == "":
        value = item.get("noProxy")
    return _text(value)


def build_proxy_plan(raw: Any) -> ProxyPlan:
    """The plan for a profile's ``proxy`` section (any shape, or missing)."""
    section = normalize_proxy_section(raw)
    entries = tuple(ProxyEntry(**item) for item in section["proxies"])
    default = None
    for item in entries:
        if item.name == section["default"]:
            default = item
            break
    if default is None and entries:
        default = entries[0]
    assignments = dict(section["assignments"])
    warnings: list[str] = []
    names = {item.name for item in entries}
    for connector, value in sorted(assignments.items()):
        if value and value.lower() not in _NONE_WORDS and value not in names:
            warnings.append(f"{connector} is assigned the proxy {value!r}, which the Proxy connector does not define; it follows the environment")
    raw_default = _text(raw.get("default")) if isinstance(raw, Mapping) else ""
    if raw_default and raw_default not in names and entries:
        warnings.append(f"the default proxy {raw_default!r} is not defined; {entries[0].name!r} is the default")
    return ProxyPlan(
        enabled=section["enabled"],
        entries=entries,
        default=default,
        assignments=assignments,
        warnings=tuple(warnings),
    )


def no_proxy_exempts(host: str, no_proxy: str) -> bool:
    """Whether a NO_PROXY list keeps ``host`` off the proxy, the way the Go CLIs decide it."""
    target = _text(host).lower().rstrip(".")
    if target.startswith("[") and target.endswith("]"):
        target = target[1:-1]
    if not target:
        return False
    if target in _LOOPBACK_HOSTS:
        return True
    for raw_entry in re.split(r"[,\s]+", no_proxy or ""):
        entry = raw_entry.strip().lower()
        if not entry:
            continue
        if entry == "*":
            return True
        for prefix in ("http://", "https://"):
            if entry.startswith(prefix):
                entry = entry[len(prefix):]
        if entry.startswith("[") and "]" in entry:
            entry = entry[1 : entry.index("]")]
        elif entry.count(":") == 1:
            entry = entry.split(":", 1)[0]
        if entry.startswith("*."):
            entry = entry[1:]
        if entry.startswith("."):
            suffix = entry[1:]
            if target == suffix or target.endswith(entry):
                return True
            continue
        if target == entry or target.endswith("." + entry):
            return True
    return False


def hostname_of(url: str) -> str:
    """The host of a URL (or of a bare host[:port]) for a NO_PROXY check."""
    text = _text(url)
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else "//" + text)
    return (parsed.hostname or "").strip().lower()
