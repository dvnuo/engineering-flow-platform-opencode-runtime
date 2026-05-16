from efp_opencode_adapter.permission_generator import profile_policy_permission_baseline, workspace_full_access_permission_baseline


def test_profile_policy_allows_atlassian_metadata_and_dry_run_commands():
    bash = profile_policy_permission_baseline()["bash"]
    assert bash["jira commands*"] == "allow"
    assert bash["jira schema*"] == "allow"
    assert bash["jira issue search*"] == "allow"
    assert bash["jira issue bulk-create *--dry-run*"] == "allow"
    assert bash["confluence commands*"] == "allow"
    assert bash["confluence page get*"] == "allow"


def test_profile_policy_asks_for_jira_bulk_create_without_dry_run():
    bash = profile_policy_permission_baseline()["bash"]
    assert bash["jira *"] == "ask"
    assert "jira issue bulk-create*" not in bash
    assert bash["jira issue bulk-create *--dry-run*"] == "allow"


def test_workspace_full_access_baseline_is_unchanged_for_atlassian():
    bash = workspace_full_access_permission_baseline()["bash"]
    assert bash == {"*": "allow"}
