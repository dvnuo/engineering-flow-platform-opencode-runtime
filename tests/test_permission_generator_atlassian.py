from fnmatch import fnmatchcase

from efp_opencode_adapter.permission_generator import profile_policy_permission_baseline, workspace_full_access_permission_baseline


def _policy_for(command: str) -> str:
    bash = profile_policy_permission_baseline()["bash"]
    matches = [(pattern, policy) for pattern, policy in bash.items() if fnmatchcase(command, pattern)]
    if not matches:
        raise AssertionError(f"no policy matched {command!r}")
    return max(matches, key=lambda item: len(item[0]))[1]


def test_profile_policy_allows_atlassian_metadata_and_dry_run_commands():
    bash = profile_policy_permission_baseline()["bash"]
    assert bash["jira commands*"] == "allow"
    assert bash["jira schema*"] == "allow"
    assert bash["jira issue search*"] == "allow"
    assert bash["jira issue bulk-validate*"] == "allow"
    assert bash["jira issue bulk-create *--dry-run*"] == "allow"
    assert bash["confluence commands*"] == "allow"
    assert bash["confluence page get*"] == "allow"


def test_profile_policy_asks_for_jira_bulk_create_without_dry_run():
    bash = profile_policy_permission_baseline()["bash"]
    assert bash["jira *"] == "ask"
    assert "jira issue bulk-create*" not in bash
    assert bash["jira issue bulk-create *--dry-run*"] == "allow"
    assert _policy_for("jira issue bulk-create --yes /workspace/uploads/issues.csv") == "ask"
    assert _policy_for("jira issue bulk-create --dry-run /workspace/uploads/issues.csv") == "allow"
    assert _policy_for("jira issue bulk-validate /workspace/uploads/issues.csv") == "allow"


def test_workspace_full_access_baseline_is_unchanged_for_atlassian():
    bash = workspace_full_access_permission_baseline()["bash"]
    assert bash == {"*": "allow"}
