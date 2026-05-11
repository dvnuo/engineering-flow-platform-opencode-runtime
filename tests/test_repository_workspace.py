import subprocess

from efp_opencode_adapter.repository_workspace import ensure_repo_checkout, parse_create_pr_repo_request, RepoRequest
from efp_opencode_adapter.settings import Settings


def test_parse_create_pr_repo_request_success():
    req = parse_create_pr_repo_request("in git repo https://github.com/dvnuo/engineering-flow-platform-portal from branch feature/20260504-opencode-integrated to develop")
    assert req and req.owner == "dvnuo" and req.repo == "engineering-flow-platform-portal"
    req_git = parse_create_pr_repo_request("in git repo https://github.com/dvnuo/engineering-flow-platform-portal.git from branch feat/x to develop")
    assert req_git and req_git.repo == "engineering-flow-platform-portal"


def test_parse_create_pr_repo_request_failures():
    assert parse_create_pr_repo_request("in git repo git@github.com:a/b.git from branch x to y") is None
    assert parse_create_pr_repo_request("in git repo https://github.com/a/b to main") is None
    assert parse_create_pr_repo_request("in git repo https://github.com/a/b from branch main") is None
    assert parse_create_pr_repo_request("in git repo https://github.com/a/b from branch feat;bad to main") is None


def test_checkout_success(monkeypatch, tmp_path):
    calls = []
    def fake_run(args, capture_output, text, shell, timeout):
        calls.append((args, shell, timeout))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    settings = Settings.from_env(opencode_url="http://x")
    object.__setattr__(settings, "workspace_repos_dir", tmp_path / "repos")
    req = RepoRequest("https://github.com/a/b", "a", "b", "feature/x", "develop")
    out = ensure_repo_checkout(settings, req)
    assert out.success is True
    assert str(tmp_path / "repos" / "a" / "b") == out.path
    assert all(isinstance(c[0], list) and c[1] is False for c in calls)


def test_checkout_dirty_workspace(monkeypatch, tmp_path):
    target = tmp_path / "repos" / "a" / "b" / ".git"
    target.mkdir(parents=True)
    def fake_run(args, capture_output, text, shell, timeout):
        if args[-2:] == ["status", "--porcelain"]:
            return subprocess.CompletedProcess(args, 0, stdout="M x.py\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    settings = Settings.from_env(opencode_url="http://x")
    object.__setattr__(settings, "workspace_repos_dir", tmp_path / "repos")
    out = ensure_repo_checkout(settings, RepoRequest("https://github.com/a/b", "a", "b", "h", "d"))
    assert out.success is False and out.error == "workspace_dirty"


def test_checkout_timeout(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1)
    monkeypatch.setattr(subprocess, "run", fake_run)
    settings = Settings.from_env(opencode_url="http://x")
    object.__setattr__(settings, "workspace_repos_dir", tmp_path / "repos")
    out = ensure_repo_checkout(settings, RepoRequest("https://github.com/a/b", "a", "b", "h", "d"))
    assert out.error == "git_timeout"


def test_checkout_auth_failed(monkeypatch, tmp_path):
    def fake_run(args, capture_output, text, shell, timeout):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="Authentication failed for https://token@github.com/a/b")
    monkeypatch.setattr(subprocess, "run", fake_run)
    settings = Settings.from_env(opencode_url="http://x")
    object.__setattr__(settings, "workspace_repos_dir", tmp_path / "repos")
    out = ensure_repo_checkout(settings, RepoRequest("https://github.com/a/b", "a", "b", "h", "d"))
    assert out.success is False and out.error in {"git_auth_failed", "git_clone_failed"}
    assert "token" not in str(out.diagnostics).lower()
