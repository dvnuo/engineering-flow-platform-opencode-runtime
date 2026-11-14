"""The member behind a request: the Portal's ``portal_user`` rendered as one system-prompt block.

An assistant's jira, confluence, gh, jenkins and aws CLIs sign in with
whatever is in the member's Portal connectors, which is usually a shared
service account. The Portal's proxy fills ``metadata.portal_user`` in on
every proxied chat request (``id``, ``username``, ``display_name`` and, when
the member has one, ``github_login``). Without a block naming that member the
model only knows the shared CLI credentials, so "my tickets" silently becomes
the service account's tickets. The block and its identity rules are the ones
the native runtime renders, word for word.

As with the "Reply language" block (``reply_language.py``), this adapter does
not build the system prompt: OpenCode does, and appends the request's
``system`` string at the very end of it for every step of the turn. The block
therefore goes into that string (``prepend_session_user_block``) and is never
a message part, since parts feed the Copilot auto-routing prompt and the
first-message conversation key.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

SESSION_USER_FIELD_MAX_CHARS = 128

SESSION_USER_IDENTITY_RULES = """Identity rules:
- The jira, confluence, gh, jenkins, and aws CLIs may authenticate with a shared service account configured in the user's Portal connectors rather than with this user's own account. `jira myself`, `confluence myself`, JQL/CQL `currentUser()`, GitHub's `@me` and `gh api user`, and similar "current user" lookups describe that service account, never the session user.
- When the user says "my", "mine", "me", "assigned to me", "my pages", "my pull requests", "my branches", or otherwise refers to themselves, filter explicitly by the session user identity above, for example JQL `assignee = "<username>"`, CQL `creator = "<username>"`, or `gh search prs --author <GitHub login>`. On GitHub the session user is the GitHub login above when one is listed, otherwise their username. If the external system needs an account id, look the user up by username or display name first.
- If the session user cannot be resolved in the external system, ask which account to use instead of silently falling back to the service account."""

# The portal_user keys that describe the member, and the block's lines in
# their rendered order.
_IDENTITY_KEYS = ("id", "username", "display_name", "github_login")
_BLOCK_FIELDS = (
    ("display name", "display_name"),
    ("username", "username"),
    ("GitHub login", "github_login"),
    ("portal user id", "id"),
)

# A GitHub login ends up inside commands such as `gh search prs --author`,
# so anything that is not shaped like one is dropped rather than rendered.
_GITHUB_LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def resolve_session_user(metadata: Mapping[str, Any] | None) -> dict[str, str] | None:
    """The member a request's metadata names, or None for today's prompt.

    Only the structured ``portal_user`` object counts. Every value is reduced
    to one short line, and ``github_login`` is kept only when it looks like a
    login. None covers everything that must not change the prompt: no
    ``portal_user`` object, and one without a ``username`` or a
    ``display_name``, since an id alone identifies nobody to the model.
    """
    if not isinstance(metadata, Mapping):
        return None
    portal_user = metadata.get("portal_user")
    if not isinstance(portal_user, Mapping):
        return None
    identity: dict[str, str] = {}
    for key in _IDENTITY_KEYS:
        value = _clean_identity_value(portal_user.get(key))
        if value:
            identity[key] = value
    github_login = identity.get("github_login")
    if github_login is not None and not _GITHUB_LOGIN_PATTERN.fullmatch(github_login):
        del identity["github_login"]
    if "username" not in identity and "display_name" not in identity:
        return None
    return identity


def render_session_user_block(identity: Mapping[str, str]) -> str:
    """The ``Session user:`` system block for ``identity``, followed by the identity rules."""
    lines = [f"- {label}: {identity[key]}" for label, key in _BLOCK_FIELDS if identity.get(key)]
    return "Session user:\n" + "\n".join(lines) + "\n\n" + SESSION_USER_IDENTITY_RULES


def prepend_session_user_block(system: str | None, identity: Mapping[str, str] | None) -> str | None:
    """``system`` led by the block, or ``system`` itself, unchanged, when no block applies.

    The block comes first and the caller's text keeps today's composition
    after a blank line; a blank ``system`` becomes the block alone.
    """
    if not identity:
        return system
    block = render_session_user_block(identity)
    if isinstance(system, str) and system.strip():
        return f"{block}\n\n{system}"
    return block


def _clean_identity_value(value: Any) -> str:
    # One line per field: every whitespace run, line breaks included, becomes
    # a single space, so a value cannot start a line of its own in the block.
    if value is None or isinstance(value, bool):
        return ""
    return " ".join(str(value).split())[:SESSION_USER_FIELD_MAX_CHARS]


__all__ = [
    "SESSION_USER_FIELD_MAX_CHARS",
    "SESSION_USER_IDENTITY_RULES",
    "prepend_session_user_block",
    "render_session_user_block",
    "resolve_session_user",
]
