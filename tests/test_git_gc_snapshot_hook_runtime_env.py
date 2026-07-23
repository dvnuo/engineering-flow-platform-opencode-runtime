"""End-to-end: an opencode snapshot object must survive a real `git gc` run
with the environment opencode is actually spawned with.

The image registers gc.recentObjectsHook at --system scope, but runtime_env
exports GIT_CONFIG_NOSYSTEM=1 for the child, so /etc/gitconfig is never read.
String-matching the Dockerfile, or configuring the hook at local repo scope,
both pass while the shipped feature does nothing; only running git with the
projected env catches it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from efp_opencode_adapter.git_cli_auth import GC_RECENT_OBJECTS_HOOK_ENV, write_git_gh_auth_assets
from efp_opencode_adapter.runtime_env import build_runtime_env_from_config, strip_managed_external_env

from test_runtime_env_git_gh import make_settings

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "scripts" / "opencode-snapshot-recent-objects"


def _git_supports_recent_objects_hook() -> bool:
    """gc.recentObjectsHook landed in git 2.42 (the image ships 2.43)."""
    result = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False)
    try:
        major, minor = (int(part) for part in result.stdout.split()[2].split(".")[:2])
    except (IndexError, ValueError):
        return False
    return (major, minor) >= (2, 42)


def _git(git_dir: Path, *args: str, work_tree: Path | None = None) -> str:
    command = ["git", f"--git-dir={git_dir}"]
    if work_tree is not None:
        command.append(f"--work-tree={work_tree}")
    command.extend(args)
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()


def _repo_with_unreferenced_tree(git_dir: Path) -> str:
    """A repo holding exactly the shape the hook protects: a tree object that
    nothing references any more (an opencode snapshot left behind by a revert)."""
    work_tree = git_dir.parent / f"{git_dir.name}-work"
    work_tree.mkdir(parents=True)
    _git(git_dir, "init", "--bare", "--quiet")

    tracked = work_tree / "snapshot.txt"
    tracked.write_text("before\n", encoding="utf-8")
    _git(git_dir, "add", "--all", work_tree=work_tree)
    unreferenced = _git(git_dir, "write-tree", work_tree=work_tree)

    tracked.write_text("after\n", encoding="utf-8")
    _git(git_dir, "add", "--all", work_tree=work_tree)
    _git(git_dir, "write-tree", work_tree=work_tree)
    return unreferenced


def _tree_exists(git_dir: Path, tree: str) -> bool:
    result = subprocess.run(
        ["git", f"--git-dir={git_dir}", "cat-file", "-e", f"{tree}^{{tree}}"],
        capture_output=True,
    )
    return result.returncode == 0


@pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="needs a real git and a POSIX shell to run the gc hook",
)
def test_snapshot_object_survives_git_gc_in_the_opencode_spawn_env(tmp_path, monkeypatch):
    if not _git_supports_recent_objects_hook():
        pytest.skip("gc.recentObjectsHook requires git >= 2.42")

    settings = make_settings(tmp_path)
    monkeypatch.setenv(GC_RECENT_OBJECTS_HOOK_ENV, f"bash {HOOK.as_posix()}")

    # Exactly what boot projection does before opencode is spawned.
    env_result = build_runtime_env_from_config(settings, {})
    write_git_gh_auth_assets(settings, env_result.env)
    env = {**strip_managed_external_env(os.environ), **env_result.env}
    if os.name == "nt":
        # The hook compares against `git rev-parse --absolute-git-dir`, which is
        # POSIX-shaped on every platform.
        for key in ("OPENCODE_DATA_DIR", "XDG_DATA_HOME"):
            env[key] = Path(env[key]).as_posix()

    # The image's `git config --system` registration cannot be the mechanism.
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"

    snapshot_git = settings.opencode_data_dir / "snapshot" / "project"
    control_git = tmp_path / "ordinary-repo"
    snapshot_tree = _repo_with_unreferenced_tree(snapshot_git)
    control_tree = _repo_with_unreferenced_tree(control_git)

    for git_dir in (snapshot_git, control_git):
        subprocess.run(
            ["git", f"--git-dir={git_dir}", "gc", "--prune=now"],
            check=True,
            capture_output=True,
            env=env,
        )

    assert _tree_exists(snapshot_git, snapshot_tree), (
        "opencode snapshot object was pruned: gc.recentObjectsHook never ran in the spawn env"
    )
    # Without this the assertion above would also hold if gc pruned nothing.
    assert not _tree_exists(control_git, control_tree), "git gc did not prune at all"
