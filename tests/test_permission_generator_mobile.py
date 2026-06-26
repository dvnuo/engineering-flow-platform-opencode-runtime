from fnmatch import fnmatchcase

from efp_opencode_adapter.permission_generator import profile_policy_permission_baseline


def _policy_for(command: str) -> str | None:
    bash = profile_policy_permission_baseline()["bash"]
    for pattern, policy in bash.items():
        if fnmatchcase(command, pattern):
            return policy
    return None


def test_profile_policy_allows_mobile_diagnostics_and_reads_but_asks_for_actions():
    bash = profile_policy_permission_baseline()["bash"]
    assert bash["mobile-auto commands*"] == "allow"
    assert bash["mobile-auto schema*"] == "allow"
    assert bash["mobile-auto doctor*"] == "allow"
    assert bash["mobile-auto auth test*"] == "allow"
    assert bash["mobile-auto observe*"] == "allow"
    assert _policy_for("mobile-auto run status --run-id run-1 --json") == "allow"
    assert _policy_for("mobile-auto run start --file bs://app --json") == "ask"
    assert _policy_for("mobile-auto tap --run-id run-1 --ref obs:e1 --json") == "ask"
