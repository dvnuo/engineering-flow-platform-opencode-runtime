from efp_opencode_adapter.permission_generator import profile_policy_permission_baseline, workspace_full_access_permission_baseline


def test_workspace_full_access_is_open():
    p = workspace_full_access_permission_baseline()
    assert p["external_directory"] == "allow"
    assert p["bash"]["*"] == "allow"


def test_profile_policy_allows_git_gh():
    p = profile_policy_permission_baseline()
    assert p["external_directory"] == "allow"
    assert p["bash"]["git *"] == "allow"
    assert p["bash"]["gh *"] == "allow"
    assert "git push *" not in p["bash"]
