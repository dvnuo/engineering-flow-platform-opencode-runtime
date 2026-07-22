from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "scripts" / "opencode-snapshot-recent-objects"


def _git(git_dir: Path, *args: str, work_tree: Path | None = None) -> str:
    command = ["git", f"--git-dir={git_dir}"]
    if work_tree is not None:
        command.append(f"--work-tree={work_tree}")
    command.extend(args)
    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _create_unreferenced_trees(root: Path) -> tuple[Path, str]:
    git_dir = root / "snapshot.git"
    work_tree = root / "work"
    work_tree.mkdir(parents=True)
    _git(git_dir, "init", "--bare", "--quiet")

    tracked = work_tree / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    _git(git_dir, "add", "--all", work_tree=work_tree)
    before = _git(git_dir, "write-tree", work_tree=work_tree)

    tracked.write_text("after\n", encoding="utf-8")
    _git(git_dir, "add", "--all", work_tree=work_tree)
    _git(git_dir, "write-tree", work_tree=work_tree)
    return git_dir, before


def _age_objects(git_dir: Path) -> None:
    old = time.time() - 8 * 24 * 60 * 60
    for path in (git_dir / "objects").rglob("*"):
        if path.is_file():
            os.utime(path, (old, old))


def _tree_exists(git_dir: Path, tree: str) -> bool:
    result = subprocess.run(
        ["git", f"--git-dir={git_dir}", "cat-file", "-e", f"{tree}^{{tree}}"],
        capture_output=True,
    )
    return result.returncode == 0


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="snapshot retention hook runs in the Linux runtime image",
)
@pytest.mark.parametrize("snapshot_root_source", ["data_dir", "xdg_data_home"])
def test_recent_objects_hook_only_preserves_opencode_snapshot_store(
    tmp_path: Path,
    snapshot_root_source: str,
):
    data_dir = tmp_path / "opencode-data"
    xdg_data_home = tmp_path / "xdg-data"
    snapshot_data_dir = (
        data_dir if snapshot_root_source == "data_dir" else xdg_data_home / "opencode"
    )
    snapshot_repo = snapshot_data_dir / "snapshot" / "project" / "worktree"
    control_repo = tmp_path / "ordinary-repo"
    snapshot_git, snapshot_tree = _create_unreferenced_trees(snapshot_repo)
    control_git, control_tree = _create_unreferenced_trees(control_repo)
    hook_command = f"bash {shlex.quote(str(HOOK))}"

    for git_dir in (snapshot_git, control_git):
        _git(git_dir, "config", "gc.recentObjectsHook", hook_command)
        _age_objects(git_dir)

    env = os.environ.copy()
    env["OPENCODE_DATA_DIR"] = str(data_dir)
    env["XDG_DATA_HOME"] = str(xdg_data_home)
    for git_dir in (snapshot_git, control_git):
        for _ in range(2):
            subprocess.run(
                ["git", f"--git-dir={git_dir}", "gc", "--prune=7.days"],
                check=True,
                env=env,
            )

    assert _tree_exists(snapshot_git, snapshot_tree)
    assert not _tree_exists(control_git, control_tree)
