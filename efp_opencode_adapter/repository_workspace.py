from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import subprocess
from typing import Any

from .settings import Settings

_URL_RE = re.compile(
    r"^in\s+git\s+repo\s+(https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:\.git)?)\s+from\s+branch\s+(\S+)\s+to\s+(\S+)\s*$",
    re.IGNORECASE,
)
_FORBIDDEN_BRANCH_CHARS = {";", "&", "|", "$", "`", "\n", "\r"}


@dataclass(frozen=True)
class RepoRequest:
    repo_url: str
    owner: str
    repo: str
    head_branch: str
    base_branch: str


@dataclass(frozen=True)
class RepoCheckoutResult:
    success: bool
    repo_url: str
    owner: str
    repo: str
    path: str
    head_branch: str
    base_branch: str
    message: str
    error: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


def parse_create_pr_repo_request(arguments: str) -> RepoRequest | None:
    if not isinstance(arguments, str):
        return None
    match = _URL_RE.match(arguments.strip())
    if not match:
        return None
    repo_url, owner, repo, head_branch, base_branch = match.groups()
    if repo.endswith(".git"):
        repo = repo[:-4]
    for branch in (head_branch, base_branch):
        if any(ch in branch for ch in _FORBIDDEN_BRANCH_CHARS):
            return None
    return RepoRequest(repo_url=repo_url, owner=owner, repo=repo, head_branch=head_branch, base_branch=base_branch)


def _run_git(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, shell=False, timeout=timeout)


def _failed(repo_request: RepoRequest, target: Path, *, error: str, message: str, diagnostics: dict[str, Any] | None = None) -> RepoCheckoutResult:
    return RepoCheckoutResult(
        success=False,
        repo_url=repo_request.repo_url,
        owner=repo_request.owner,
        repo=repo_request.repo,
        path=str(target),
        head_branch=repo_request.head_branch,
        base_branch=repo_request.base_branch,
        message=message,
        error=error,
        diagnostics=diagnostics or {},
    )


def ensure_repo_checkout(settings: Settings, repo_request: RepoRequest) -> RepoCheckoutResult:
    repos_dir = getattr(settings, "workspace_repos_dir", settings.workspace_dir / "repos")
    timeout = float(getattr(settings, "git_checkout_timeout_seconds", 120.0))
    target = Path(repos_dir) / repo_request.owner / repo_request.repo
    try:
        is_git_repo = (target / ".git").exists()
        if not is_git_repo:
            target.parent.mkdir(parents=True, exist_ok=True)
            clone = _run_git(["git", "clone", "--origin", "origin", repo_request.repo_url, str(target)], timeout)
            if clone.returncode != 0:
                combined = f"{clone.stdout}\n{clone.stderr}".lower()
                error = "git_auth_failed" if any(x in combined for x in ["authentication", "permission denied", "could not read", "not found"]) else "git_clone_failed"
                return _failed(repo_request, target, error=error, message="Repository checkout failed during clone.", diagnostics={"returncode": clone.returncode})
        else:
            status = _run_git(["git", "-C", str(target), "status", "--porcelain"], timeout)
            if status.returncode != 0:
                return _failed(repo_request, target, error="git_status_failed", message="Repository checkout failed during status inspection.", diagnostics={"returncode": status.returncode})
            if status.stdout.strip():
                return _failed(repo_request, target, error="workspace_dirty", message="Repository checkout failed: workspace is dirty.")
            set_url = _run_git(["git", "-C", str(target), "remote", "set-url", "origin", repo_request.repo_url], timeout)
            if set_url.returncode != 0:
                return _failed(repo_request, target, error="git_remote_update_failed", message="Repository checkout failed while updating origin URL.", diagnostics={"returncode": set_url.returncode})

        for branch in (repo_request.head_branch, repo_request.base_branch):
            fetched = _run_git(["git", "-C", str(target), "fetch", "origin", branch], timeout)
            if fetched.returncode != 0:
                combined = f"{fetched.stdout}\n{fetched.stderr}".lower()
                error = "git_auth_failed" if any(x in combined for x in ["authentication", "permission denied", "could not read", "not found"]) else "git_fetch_failed"
                return _failed(repo_request, target, error=error, message=f"Repository checkout failed while fetching branch {branch}.", diagnostics={"returncode": fetched.returncode, "branch": branch})

        checkout = _run_git(["git", "-C", str(target), "checkout", "-B", repo_request.head_branch, f"origin/{repo_request.head_branch}"], timeout)
        if checkout.returncode != 0:
            return _failed(repo_request, target, error="git_checkout_failed", message="Repository checkout failed while checking out head branch.", diagnostics={"returncode": checkout.returncode})

        return RepoCheckoutResult(success=True, repo_url=repo_request.repo_url, owner=repo_request.owner, repo=repo_request.repo, path=str(target), head_branch=repo_request.head_branch, base_branch=repo_request.base_branch, message="Repository checkout prepared.")
    except FileNotFoundError:
        return _failed(repo_request, target, error="git_not_available", message="Repository checkout failed: git is not available.")
    except subprocess.TimeoutExpired:
        return _failed(repo_request, target, error="git_timeout", message="Repository checkout failed due to git timeout.")
