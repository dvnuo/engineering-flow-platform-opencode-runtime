"""Git repositories cloned in the assistant's workspace.

When a member fills in a starter card or answers a question that wants a
branch, the branch the assistant is already working on is usually the one they
mean. Portal cannot see the workspace, so the runtime lists what is checked
out there and Portal offers it as "branches in this workspace".

The listing runs inside a chat runtime, next to an agent that may be using the
very same repositories at that moment, so it is deliberately harmless:

- It only asks git questions. Nothing is fetched, so `ahead` is measured
  against the refs already on disk, and `GIT_OPTIONAL_LOCKS=0` keeps even
  `git status` from refreshing the index behind the agent's back.
- It is bounded. Each git command has a short timeout and the whole listing
  has a time budget; when the budget runs out the caller gets what was
  collected, marked `truncated`, rather than a slow or failed request.
- It accepts no path. The places scanned are fixed, and a directory that
  resolves outside the workspace (through a link) is ignored.

One repository that git cannot read never fails the listing: it is left out,
or reported with the fields that could be resolved.

The native runtime answers the same endpoint with the same shape.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

MAX_REPOS = 30
GIT_COMMAND_TIMEOUT_SECONDS = 3.0
LISTING_BUDGET_SECONDS = 8.0

# `[user@]host:path`, the form `git clone git@github.com:org/repo.git` stores.
# Only tried on a remote with no "://". The host cannot contain a slash, so a
# local path does not match; a one-letter "host" is a Windows drive
# (C:\repos\x) and is rejected by the caller.
_SCP_LIKE_RE = re.compile(r"(?:[^@/\\]+@)?([^@/\\:]+):(.+)")

_ORIGIN_REFS = "refs/remotes/origin/"


class _BudgetExhausted(Exception):
    """The listing ran out of time; what is collected so far is the answer."""


class _Git:
    """Runs read-only git queries against one shared deadline."""

    def __init__(self, budget_seconds: float, command_timeout_seconds: float) -> None:
        self._deadline = time.monotonic() + max(0.0, budget_seconds)
        self._command_timeout = command_timeout_seconds
        self._env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}

    def remaining(self) -> float:
        return self._deadline - time.monotonic()

    def run(self, repo_dir: Path, *args: str) -> str | None:
        """Stripped stdout of `git <args>` in `repo_dir`; None when it failed.

        A failure is an answer here, not an error: "no upstream", "detached
        HEAD" and "no origin" all arrive as a non-zero exit. A command that is
        cut short because the listing's budget ran out raises instead, so the
        caller does not mistake "no time left" for "no upstream".
        """
        remaining = self.remaining()
        if remaining <= 0:
            raise _BudgetExhausted
        # A directory whose `.git` entry is not a repository would otherwise
        # make git walk up and answer for whichever repository encloses the
        # workspace. The ceiling stops the search at the directory itself.
        env = {**self._env, "GIT_CEILING_DIRECTORIES": str(repo_dir.parent)}
        try:
            completed = subprocess.run(
                # core.fsmonitor=false: `git status` must not start a file
                # watcher (or run a configured hook) on behalf of a listing.
                ["git", "-C", str(repo_dir), "-c", "core.fsmonitor=false", *args],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                env=env,
                encoding="utf-8",
                errors="replace",
                shell=False,
                timeout=min(self._command_timeout, remaining),
            )
        except subprocess.TimeoutExpired:
            if self.remaining() <= 0:
                raise _BudgetExhausted from None
            return None
        except OSError:
            # git is not installed or cannot be started: every repository is
            # then unreadable, which the caller reports as an empty listing.
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout.strip()


def list_workspace_repos(
    workspace_root: Path,
    extra_repos_root: Path | None = None,
    *,
    max_repos: int = MAX_REPOS,
    budget_seconds: float = LISTING_BUDGET_SECONDS,
    command_timeout_seconds: float = GIT_COMMAND_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Return {"repos": [...], "truncated": bool} for one workspace.

    `extra_repos_root` is the runtime's configured repositories directory. It
    is scanned (two levels, like `repos/`) only when it exists inside the
    workspace; by default it *is* `<workspace>/repos` and adds nothing.

    Repositories come back newest commit first, at most `max_repos` of them.
    `truncated` says the listing is not the whole picture: there were more
    repositories than the cap, or the time budget ran out.

    The work is done in two passes so that a workspace with many clones still
    gets its newest ones described: the first pass reads only each HEAD commit
    (one git command per repository, enough to sort), the second describes the
    repositories that made the cut. The first pass may use at most half of the
    budget, so there is always time left to describe something.
    """
    try:
        root = Path(workspace_root).resolve()
    except (OSError, RuntimeError):
        return {"repos": [], "truncated": False}

    git = _Git(budget_seconds, command_timeout_seconds)
    truncated = False

    # (commit timestamp, workspace-relative path, directory, short hash)
    dated: list[tuple[int, str, Path, str]] = []
    seen: set[Path] = set()
    try:
        for candidate in _candidate_dirs(root, _inside(root, extra_repos_root)):
            if git.remaining() <= budget_seconds / 2:
                truncated = True
                break
            repo_dir = _repository_dir(candidate, root, seen)
            if repo_dir is None:
                continue
            rel_path = repo_dir.relative_to(root).as_posix()
            commit = _head_commit(git, repo_dir)
            if commit is None:
                # No commit yet, a broken clone, or git refusing to read it.
                # There is no branch to offer either way.
                logger.debug("Skipping workspace repository %s: its HEAD commit cannot be read", rel_path)
                continue
            head, committed_at = commit
            dated.append((committed_at, rel_path, repo_dir, head))
    except _BudgetExhausted:
        truncated = True

    dated.sort(key=lambda item: (-item[0], item[1]))
    if len(dated) > max_repos:
        truncated = True

    repos: list[dict[str, Any]] = []
    try:
        for committed_at, rel_path, repo_dir, head in dated[: max(0, max_repos)]:
            repos.append(_describe(git, repo_dir, rel_path, head, committed_at))
    except _BudgetExhausted:
        # A half-described repository would report "detached, no upstream"
        # for fields nobody had time to read, so it is left out instead.
        truncated = True
    return {"repos": repos, "truncated": truncated}


def parse_remote_url(raw: str | None) -> tuple[str, str, str]:
    """Split an origin URL into (remote_url, host, repo).

    `remote_url` is the URL without its userinfo: a clone made with a token
    in the URL must not hand that token to a browser. `host` and `repo`
    ("org/name", no leading slash, no trailing ".git") are what Portal matches
    against its connectors; both are "" for a remote that is not on a host
    (a local path) or that cannot be parsed.
    """
    url = (raw or "").strip()
    if not url:
        return "", "", ""

    if "://" in url:
        try:
            parts = urlsplit(url)
            host = parts.hostname or ""
            port = parts.port
        except ValueError:
            # A malformed port or address. Echoing it as written could echo a
            # credential, so it is reported as no remote at all.
            return "", "", ""
        netloc = f"[{host}]" if ":" in host else host
        if port is not None:
            netloc = f"{netloc}:{port}"
        # Query and fragment are dropped with the userinfo: git does not use
        # them, and a token is the only thing likely to be found there.
        remote_url = f"{parts.scheme}://{netloc}{parts.path}"
        if not host:
            return remote_url, "", ""
        return remote_url, host, _repo_path(parts.path)

    match = _SCP_LIKE_RE.fullmatch(url)
    if match is None or len(match.group(1)) < 2:
        # A local path (another clone on disk): nothing to strip or to parse.
        return url, "", ""
    host, path = match.groups()
    return f"{host}:{path}", host.lower(), _repo_path(path)


def _repo_path(path: str) -> str:
    repo = path.strip("/")
    return repo[: -len(".git")] if repo.endswith(".git") else repo


def _inside(root: Path, directory: Path | None) -> Path | None:
    """`directory` resolved, when it is an existing directory inside `root`."""
    if directory is None:
        return None
    try:
        resolved = Path(directory).resolve()
    except (OSError, RuntimeError):
        return None
    if not resolved.is_dir() or not resolved.is_relative_to(root):
        return None
    return resolved


def _candidate_dirs(root: Path, extra_repos_root: Path | None) -> Iterator[Path]:
    """Every directory that may be a repository, in scan order.

    These are the places a clone is known to land: `repos/<name>`,
    `repos/<owner>/<name>` (where this runtime checks repositories out, or
    the same two levels of the directory it was configured to use instead),
    `.efp_runtime/repositories/<name>` and the workspace root's own children.
    The workspace is not searched recursively: a deep walk has no bound, and a
    repository's working tree can hold thousands of directories.
    """
    repos_roots = [root / "repos"]
    if extra_repos_root is not None:
        repos_roots.append(extra_repos_root)
    for repos_root in repos_roots:
        children = _child_dirs(repos_root)
        yield from children
        for child in children:
            yield from _child_dirs(child)
    yield from _child_dirs(root / ".efp_runtime" / "repositories")
    yield from _child_dirs(root)


def _child_dirs(directory: Path) -> list[Path]:
    try:
        return sorted(child for child in directory.iterdir() if child.is_dir())
    except OSError:
        # Most often simply absent; unreadable is treated the same way.
        return []


def _repository_dir(candidate: Path, root: Path, seen: set[Path]) -> Path | None:
    """The resolved directory when `candidate` is a repository to list.

    Resolving first is what keeps a link from leading the listing out of the
    workspace, and what stops a clone reached twice (the configured
    repositories directory is `repos/` unless it was changed) from being
    listed twice. The workspace root itself is never listed.
    """
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return None
    if resolved in seen:
        return None
    seen.add(resolved)
    if resolved == root or not resolved.is_relative_to(root):
        return None
    # A worktree or a submodule has a `.git` file rather than a directory.
    if not (resolved / ".git").exists():
        return None
    return resolved


def _head_commit(git: _Git, repo_dir: Path) -> tuple[str, int] | None:
    """(short hash, committer timestamp) of HEAD, None when it has no commit."""
    # rev-list rather than log: it ignores log.showSignature and friends, so
    # the output is always "commit <hash>" followed by the line asked for.
    output = git.run(repo_dir, "rev-list", "-1", "--format=%h %ct", "HEAD")
    if not output:
        return None
    head, _, stamp = output.splitlines()[-1].partition(" ")
    if not head or not stamp.isdigit():
        return None
    return head, int(stamp)


def _describe(git: _Git, repo_dir: Path, rel_path: str, head: str, committed_at: int) -> dict[str, Any]:
    branch = git.run(repo_dir, "symbolic-ref", "--short", "-q", "HEAD") or ""
    # Full ref names are kept for the comparison below: a short name such as
    # "origin/main" could also be a local branch somebody named that way.
    upstream_ref = git.run(repo_dir, "rev-parse", "--symbolic-full-name", "@{upstream}") or ""
    origin_head = git.run(repo_dir, "symbolic-ref", "-q", f"{_ORIGIN_REFS}HEAD") or ""
    default_branch = origin_head[len(_ORIGIN_REFS):] if origin_head.startswith(_ORIGIN_REFS) else ""
    # The configured value, not `git remote get-url`: that one applies the
    # url.<base>.insteadOf rewrites of whoever runs it, and Portal wants the
    # address the clone was made from.
    remote_url, host, repo = parse_remote_url(git.run(repo_dir, "config", "--get", "remote.origin.url"))

    ahead: int | None = None
    base = upstream_ref or (f"{_ORIGIN_REFS}{default_branch}" if default_branch else "")
    if base:
        count = git.run(repo_dir, "rev-list", "--count", f"{base}..HEAD")
        if count is not None and count.isdigit():
            ahead = int(count)

    # Tracked files only: untracked build output is not "work in progress on
    # this branch", and walking it is what makes status slow.
    status = git.run(repo_dir, "status", "--porcelain", "--untracked-files=no")

    return {
        "path": rel_path,
        "name": repo_dir.name,
        "remote_url": remote_url,
        "host": host,
        "repo": repo,
        "branch": branch,
        "head": head,
        "upstream": _short_ref(upstream_ref),
        "default_branch": default_branch,
        "ahead": ahead,
        "dirty": None if status is None else bool(status),
        "last_commit_at": _iso_utc(committed_at),
    }


def _short_ref(ref: str) -> str:
    for prefix in ("refs/remotes/", "refs/heads/"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


def _iso_utc(timestamp: int) -> str:
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""
