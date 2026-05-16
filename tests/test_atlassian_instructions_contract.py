from efp_opencode_adapter.opencode_config import ATLASSIAN_INSTRUCTIONS_CONTENT


def test_atlassian_instructions_use_concrete_schema_commands():
    content = ATLASSIAN_INSTRUCTIONS_CONTENT
    required = [
        "jira commands --json",
        "jira schema issue.map-csv --json",
        "jira schema issue.bulk-create --json",
        "jira issue map-csv",
        "jira issue bulk-create --dry-run",
        "jira issue bulk-create --yes",
        "confluence commands --json",
        "confluence schema <command> --json",
    ]
    for token in required:
        assert token in content


def test_atlassian_instructions_avoid_ambiguous_or_portal_specific_text():
    content = ATLASSIAN_INSTRUCTIONS_CONTENT
    forbidden = [
        "jira schema --json",
        "confluence schema --json",
        "EFP",
    ]
    for token in forbidden:
        assert token not in content
