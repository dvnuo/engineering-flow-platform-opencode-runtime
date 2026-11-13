"""Git repositories listed for Portal's "branches in this workspace" picker.

The listing shells out to git, so these tests build real repositories under
tmp_path instead of faking subprocess output: what is being checked is that the
git questions asked are the right ones.
"""
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from efp_opencode_adapter import workspace_repos
from efp_opencode_adapter.server import create_app
from efp_opencode_adapter.settings import Settings
from efp_opencode_adapter.workspace_repos import MAX_REPOS, list_workspace_repos, parse_remote_url
from test_t06_helpers import FakeOpenCodeClient

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

COMMITTED_AT = int(datetime(2026, 11, 12, 8, 10, tzinfo=timezone.utc).timestamp())
HOUR = 3600


@pytest.fixture(autouse=True)
def hermetic_git(tmp_path, monkeypatch):
    # The developer's own git configuration (signing, hooks, a default branch
    # name, URL rewrites) must not decide what these repositories look like.
    config = tmp_path / "gitconfig"
    config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def workspace(tmp_path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    return root


def _git(cwd: Path, *args: str, when: int | None = None) -> str:
    env = dict(os.environ)
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"{when} +0000"
    completed = subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, encoding="utf-8"
    )
    return completed.stdout.strip()


def _commit(repo: Path, name: str, *, when: int = COMMITTED_AT) -> None:
    (repo / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
    _git(repo, "add", f"{name}.txt")
    _git(repo, "commit", "-q", "-m", name, when=when)


def _make_repo(path: Path, *, when: int = COMMITTED_AT, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    # Not `init -b`: this spelling works on every git, whatever it defaults to.
    _git(path, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    _git(path, "config", "user.name", "Test Author")
    _git(path, "config", "user.email", "author@example.com")
    _commit(path, "first", when=when)
    return path


def _make_origin(tmp_path: Path, repo: Path) -> Path:
    """A bare repository outside the workspace that `repo` pushes main to."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare")
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "main")
    # What a clone records by itself; given a branch name it stays offline.
    _git(repo, "remote", "set-head", "origin", "main")
    return origin


def _only(payload: dict) -> dict:
    assert len(payload["repos"]) == 1, payload
    return payload["repos"][0]


def _paths(payload: dict) -> list[str]:
    return [repo["path"] for repo in payload["repos"]]


# ------------------------------------------------------------ where it looks


def test_repo_under_repos_is_described_in_full(workspace):
    repo = _make_repo(workspace / "repos" / "efp-portal")

    payload = list_workspace_repos(workspace)

    assert payload == {
        "repos": [
            {
                "path": "repos/efp-portal",
                "name": "efp-portal",
                # No origin: nothing to parse, and nothing to be ahead of.
                "remote_url": "",
                "host": "",
                "repo": "",
                "branch": "main",
                "head": _git(repo, "rev-parse", "--short", "HEAD"),
                "upstream": "",
                "default_branch": "",
                "ahead": None,
                "dirty": False,
                "last_commit_at": "2026-11-12T08:10:00+00:00",
            }
        ],
        "truncated": False,
    }


def test_nested_owner_and_name_under_repos_is_found(workspace):
    # Where this runtime's own checkout puts a repository.
    _make_repo(workspace / "repos" / "org" / "efp-tools")

    repo = _only(list_workspace_repos(workspace))

    assert repo["path"] == "repos/org/efp-tools"
    assert repo["name"] == "efp-tools"


def test_direct_child_of_the_workspace_is_found(workspace):
    _make_repo(workspace / "scratch-clone")

    assert _paths(list_workspace_repos(workspace)) == ["scratch-clone"]


def test_native_runtime_repositories_directory_is_scanned(workspace):
    _make_repo(workspace / ".efp_runtime" / "repositories" / "efp-agents")

    assert _paths(list_workspace_repos(workspace)) == [".efp_runtime/repositories/efp-agents"]


def test_directories_that_are_not_repositories_are_ignored(workspace):
    (workspace / "repos" / "notes").mkdir(parents=True)
    (workspace / "repos" / "notes" / "todo.md").write_text("later\n", encoding="utf-8")
    (workspace / "docs").mkdir()
    (workspace / "repos" / "stray-file").write_text("not a directory\n", encoding="utf-8")
    _make_repo(workspace / "repos" / "real")

    assert _paths(list_workspace_repos(workspace)) == ["repos/real"]


def test_repositories_deeper_than_the_scanned_places_are_not_listed(workspace):
    # The workspace is not walked: three levels under repos/, or two under
    # any other directory, is out of reach.
    _make_repo(workspace / "repos" / "a" / "b" / "too-deep")
    _make_repo(workspace / "projects" / "elsewhere")

    assert list_workspace_repos(workspace) == {"repos": [], "truncated": False}


def test_the_workspace_root_itself_is_never_listed(workspace):
    _make_repo(workspace)
    _make_repo(workspace / "repos" / "inner")

    # Not even when the configured repositories directory is the root.
    assert _paths(list_workspace_repos(workspace)) == ["repos/inner"]
    assert _paths(list_workspace_repos(workspace, workspace)) == ["repos/inner"]


def test_missing_workspace_yields_an_empty_listing(tmp_path):
    assert list_workspace_repos(tmp_path / "absent") == {"repos": [], "truncated": False}


def _link_directory(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except OSError:
        if os.name != "nt":
            pytest.skip("this account cannot create symbolic links")
    # Windows keeps symbolic links for privileged accounts; a junction is the
    # same thing to the code under test and needs no privilege.
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    if made.returncode != 0:
        pytest.skip("this account can create neither symbolic links nor junctions")


def test_a_link_leading_out_of_the_workspace_is_skipped(tmp_path, workspace):
    outside = _make_repo(tmp_path / "outside" / "secret")
    (workspace / "repos").mkdir()
    _link_directory(workspace / "repos" / "linked", outside)

    assert list_workspace_repos(workspace) == {"repos": [], "truncated": False}


def test_a_link_inside_the_workspace_lists_the_repository_once(workspace):
    real = _make_repo(workspace / "real")
    (workspace / "repos").mkdir()
    _link_directory(workspace / "repos" / "alias", real)

    # Under the path it really has, so two names never mean two repositories.
    assert _paths(list_workspace_repos(workspace)) == ["real"]


# ------------------------------------------------- configured repos directory


def test_extra_repos_root_is_scanned_two_levels_deep(workspace):
    _make_repo(workspace / "src" / "flat")
    _make_repo(workspace / "src" / "org" / "nested")

    assert list_workspace_repos(workspace) == {"repos": [], "truncated": False}
    assert _paths(list_workspace_repos(workspace, workspace / "src")) == ["src/flat", "src/org/nested"]


def test_default_extra_repos_root_does_not_list_anything_twice(workspace):
    # Settings.workspace_repos_dir is <workspace>/repos unless overridden.
    _make_repo(workspace / "repos" / "org" / "app")

    assert _paths(list_workspace_repos(workspace, workspace / "repos")) == ["repos/org/app"]


def test_extra_repos_root_outside_the_workspace_is_ignored(tmp_path, workspace):
    _make_repo(tmp_path / "elsewhere" / "app")

    assert list_workspace_repos(workspace, tmp_path / "elsewhere") == {"repos": [], "truncated": False}
    assert list_workspace_repos(workspace, tmp_path / "does-not-exist") == {"repos": [], "truncated": False}


# ------------------------------------------------------------ what it reports


def test_detached_head_reports_an_empty_branch(workspace):
    repo = _make_repo(workspace / "repos" / "pinned")
    _git(repo, "checkout", "-q", "--detach")

    listed = _only(list_workspace_repos(workspace))

    assert listed["branch"] == ""
    assert listed["upstream"] == ""
    assert listed["head"] == _git(repo, "rev-parse", "--short", "HEAD")


def test_origin_credentials_are_stripped(workspace):
    repo = _make_repo(workspace / "repos" / "efp-portal")
    _git(repo, "remote", "add", "origin", "https://bot:s3cr3t-token@github.com/org/efp-portal.git")

    payload = list_workspace_repos(workspace)
    listed = _only(payload)

    assert listed["remote_url"] == "https://github.com/org/efp-portal.git"
    assert listed["host"] == "github.com"
    assert listed["repo"] == "org/efp-portal"
    assert "s3cr3t-token" not in json.dumps(payload)
    assert "bot" not in json.dumps(payload)


def test_scp_like_origin_is_parsed_to_host_and_repo(workspace):
    repo = _make_repo(workspace / "repos" / "efp-tools")
    _git(repo, "remote", "add", "origin", "git@github.example.com:org/efp-tools.git")

    listed = _only(list_workspace_repos(workspace))

    assert listed["host"] == "github.example.com"
    assert listed["repo"] == "org/efp-tools"
    assert listed["remote_url"] == "github.example.com:org/efp-tools.git"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://github.com/org/efp-portal.git", ("https://github.com/org/efp-portal.git", "github.com", "org/efp-portal")),
        ("https://github.com/org/efp-portal", ("https://github.com/org/efp-portal", "github.com", "org/efp-portal")),
        ("https://x-access-token:ghs_abc@github.com/org/app.git", ("https://github.com/org/app.git", "github.com", "org/app")),
        ("https://token@GitHub.com/org/app.git/", ("https://github.com/org/app.git/", "github.com", "org/app")),
        ("https://git.example.com:8443/team/app.git?token=abc#frag", ("https://git.example.com:8443/team/app.git", "git.example.com", "team/app")),
        ("ssh://git@github.com/org/app.git", ("ssh://github.com/org/app.git", "github.com", "org/app")),
        ("ssh://git@git.example.com:7999/proj/app.git", ("ssh://git.example.com:7999/proj/app.git", "git.example.com", "proj/app")),
        ("git@github.com:org/app.git", ("github.com:org/app.git", "github.com", "org/app")),
        ("github.com:org/app", ("github.com:org/app", "github.com", "org/app")),
        ("user:pass@host.example:org/app.git", ("host.example:org/app.git", "host.example", "org/app")),
        # Remotes that are not on a host: shown as they are, nothing parsed.
        ("/srv/git/app.git", ("/srv/git/app.git", "", "")),
        ("../app.git", ("../app.git", "", "")),
        ("C:\\repos\\app.git", ("C:\\repos\\app.git", "", "")),
        ("C:/repos/app.git", ("C:/repos/app.git", "", "")),
        ("file:///srv/git/app.git", ("file:///srv/git/app.git", "", "")),
        # Unparseable: never echoed back, it may hold a credential.
        ("https://user:pass@host:notaport/org/app.git", ("", "", "")),
        ("", ("", "", "")),
        (None, ("", "", "")),
    ],
)
def test_parse_remote_url(raw, expected):
    assert parse_remote_url(raw) == expected


def test_ahead_counts_commits_the_upstream_does_not_have(tmp_path, workspace):
    repo = _make_repo(workspace / "repos" / "app")
    origin = _make_origin(tmp_path, repo)
    _git(repo, "checkout", "-q", "-b", "feat/x")
    _git(repo, "push", "-q", "-u", "origin", "feat/x")
    _commit(repo, "second")
    _commit(repo, "third")
    _commit(repo, "fourth")

    listed = _only(list_workspace_repos(workspace))

    assert listed["branch"] == "feat/x"
    assert listed["upstream"] == "origin/feat/x"
    assert listed["default_branch"] == "main"
    assert listed["ahead"] == 3
    # A local path is a remote too; it just has no host to match.
    assert listed["remote_url"] == str(origin)
    assert (listed["host"], listed["repo"]) == ("", "")


def test_ahead_falls_back_to_the_default_branch_when_nothing_is_tracked(tmp_path, workspace):
    repo = _make_repo(workspace / "repos" / "app")
    _make_origin(tmp_path, repo)
    _git(repo, "checkout", "-q", "-b", "feat/local-only")
    _commit(repo, "second")
    _commit(repo, "third")

    listed = _only(list_workspace_repos(workspace))

    assert listed["branch"] == "feat/local-only"
    assert listed["upstream"] == ""
    assert listed["default_branch"] == "main"
    assert listed["ahead"] == 2


def test_ahead_is_zero_when_the_branch_matches_its_upstream(tmp_path, workspace):
    repo = _make_repo(workspace / "repos" / "app")
    _make_origin(tmp_path, repo)

    listed = _only(list_workspace_repos(workspace))

    assert listed["upstream"] == "origin/main"
    assert listed["ahead"] == 0


def test_dirty_follows_tracked_files_only(workspace):
    repo = _make_repo(workspace / "repos" / "app")

    (repo / "untracked.log").write_text("build output\n", encoding="utf-8")
    assert _only(list_workspace_repos(workspace))["dirty"] is False

    (repo / "first.txt").write_text("edited\n", encoding="utf-8")
    assert _only(list_workspace_repos(workspace))["dirty"] is True


# ----------------------------------------------------------- order and limits


def test_newest_commit_comes_first_and_the_cap_marks_truncation(workspace):
    _make_repo(workspace / "repos" / "oldest", when=COMMITTED_AT - 2 * HOUR)
    _make_repo(workspace / "newest", when=COMMITTED_AT)
    _make_repo(workspace / "repos" / "org" / "middle", when=COMMITTED_AT - HOUR)

    everything = list_workspace_repos(workspace)
    assert _paths(everything) == ["newest", "repos/org/middle", "repos/oldest"]
    assert everything["truncated"] is False

    capped = list_workspace_repos(workspace, max_repos=2)
    assert _paths(capped) == ["newest", "repos/org/middle"]
    assert capped["truncated"] is True

    assert MAX_REPOS == 30


def test_an_exhausted_budget_returns_what_was_collected_as_truncated(workspace):
    _make_repo(workspace / "repos" / "app")

    assert list_workspace_repos(workspace, budget_seconds=0) == {"repos": [], "truncated": True}


def test_an_empty_workspace_is_not_truncated_even_without_a_budget(workspace):
    assert list_workspace_repos(workspace, budget_seconds=0) == {"repos": [], "truncated": False}


def test_every_git_call_is_read_only_offline_and_bounded(tmp_path, workspace, monkeypatch):
    repo = _make_repo(workspace / "repos" / "app")
    _make_origin(tmp_path, repo)
    real_run = subprocess.run
    calls = []

    def spy(args, **kwargs):
        calls.append((args, kwargs))
        return real_run(args, **kwargs)

    monkeypatch.setattr(workspace_repos.subprocess, "run", spy)

    assert _paths(list_workspace_repos(workspace)) == ["repos/app"]

    assert calls
    for args, kwargs in calls:
        assert args[:5] == ["git", "-C", str(repo.resolve()), "-c", "core.fsmonitor=false"]
        command, operands = args[5], [arg for arg in args[6:] if not arg.startswith("-")]
        # Queries only: nothing that fetches, and nothing that writes.
        assert command in {"rev-list", "symbolic-ref", "rev-parse", "config", "status"}
        if command == "config":
            assert args[6] == "--get"
        if command == "symbolic-ref":
            # With a second operand it would repoint the ref instead of reading it.
            assert len(operands) == 1
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
        assert 0 < kwargs["timeout"] <= 3.0
        assert kwargs.get("shell") is False


# ----------------------------------------------------------- bad repositories


def test_a_broken_repository_is_skipped_without_hiding_its_neighbours(workspace):
    # The workspace is itself a repository here: a directory with a useless
    # `.git` must not be answered for by the repository that encloses it.
    _make_repo(workspace)
    _make_repo(workspace / "repos" / "good")
    (workspace / "repos" / "broken" / ".git").mkdir(parents=True)

    assert _paths(list_workspace_repos(workspace)) == ["repos/good"]


def test_a_repository_without_a_commit_is_skipped(workspace):
    empty = workspace / "repos" / "fresh"
    empty.mkdir(parents=True)
    _git(empty, "init", "-q")

    assert list_workspace_repos(workspace) == {"repos": [], "truncated": False}


def test_a_status_that_times_out_reports_dirty_as_unknown(workspace, monkeypatch):
    _make_repo(workspace / "repos" / "huge")
    real_run = subprocess.run

    def slow_status(args, **kwargs):
        if "status" in args:
            raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs["timeout"])
        return real_run(args, **kwargs)

    monkeypatch.setattr(workspace_repos.subprocess, "run", slow_status)

    payload = list_workspace_repos(workspace)

    listed = _only(payload)
    assert listed["dirty"] is None
    assert listed["branch"] == "main"
    assert payload["truncated"] is False


def test_missing_git_yields_an_empty_listing(workspace, monkeypatch):
    _make_repo(workspace / "repos" / "app")

    def no_git(args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(workspace_repos.subprocess, "run", no_git)

    assert list_workspace_repos(workspace) == {"repos": [], "truncated": False}


# ------------------------------------------------------------------- endpoint


async def _client() -> TestClient:
    app = create_app(Settings.from_env(), opencode_client=FakeOpenCodeClient())
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_endpoint_lists_the_workspace_repositories():
    settings = Settings.from_env()
    repo = _make_repo(settings.workspace_dir / "repos" / "org" / "efp-portal")
    _git(repo, "remote", "add", "origin", "https://token@github.com/org/efp-portal.git")
    client = await _client()

    resp = await client.get("/api/workspace/repos")
    body = await resp.json()
    await client.close()

    assert resp.status == 200
    assert body["truncated"] is False
    assert [(r["path"], r["branch"], r["host"], r["repo"]) for r in body["repos"]] == [
        ("repos/org/efp-portal", "main", "github.com", "org/efp-portal")
    ]
    assert body["repos"][0]["remote_url"] == "https://github.com/org/efp-portal.git"


async def test_endpoint_scans_the_configured_repositories_directory(monkeypatch):
    workspace_dir = Settings.from_env().workspace_dir
    monkeypatch.setenv("EFP_WORKSPACE_REPOS_DIR", str(workspace_dir / "checkouts"))
    _make_repo(workspace_dir / "checkouts" / "org" / "app")
    client = await _client()

    resp = await client.get("/api/workspace/repos")
    body = await resp.json()
    await client.close()

    assert resp.status == 200
    assert [r["path"] for r in body["repos"]] == ["checkouts/org/app"]


async def test_endpoint_accepts_no_path_from_the_caller(tmp_path):
    # Nothing a caller sends can point the scan somewhere else.
    _make_repo(tmp_path / "elsewhere" / "repos" / "app")
    client = await _client()

    resp = await client.get(
        "/api/workspace/repos",
        params={"path": str(tmp_path / "elsewhere"), "root": str(tmp_path / "elsewhere")},
    )
    body = await resp.json()
    await client.close()

    assert resp.status == 200
    assert body == {"repos": [], "truncated": False}


async def test_endpoint_degrades_to_an_empty_listing_when_the_scan_raises(monkeypatch):
    from efp_opencode_adapter import workspace_repos_api

    def boom(*args, **kwargs):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(workspace_repos_api, "list_workspace_repos", boom)
    client = await _client()

    resp = await client.get("/api/workspace/repos")
    body = await resp.json()
    await client.close()

    assert resp.status == 200
    assert body == {"repos": [], "truncated": False}
