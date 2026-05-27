import copy

from efp_opencode_adapter.task_prompts import build_task_prompt


def test_github_prompt_and_alias():
    p = build_task_prompt(task_id='t1', task_type='github_review_task', input_payload={'repo': 'x', 'pull_number': 1, 'head_sha': 'h'}, metadata={'portal_head_sha': 'h'})
    assert 'superseded_by_new_head_sha' in p
    assert 'Do not write back to GitHub' in p
    assert 'OpenCode runtime permission profile and OpenCode permission policy allow mutation tools' in p
    p2 = build_task_prompt(task_id='t1', task_type='github_pr_review', input_payload={}, metadata={})
    assert 'GitHub PR review task fields' in p2
    assert 'allowed tools/actions' not in p2


def test_other_prompts():
    p = build_task_prompt(task_id='t2', task_type='jira_workflow_review_task', input_payload={'issue_key': 'ABC-1'}, metadata={'workflow_rule_id': 'w'})
    assert 'issue_key' in p and 'Do not transition or reassign Jira issues' in p
    assert 'OpenCode runtime permission profile and OpenCode permission policy allow the specific mutation action' in p
    d = build_task_prompt(task_id='t3', task_type='delegation_task', input_payload={'group_id': 'g'}, metadata={})
    assert 'leader_agent_id' in d and 'expected_output_schema' in d
    b = build_task_prompt(task_id='t4', task_type='bundle_action_task', input_payload={}, metadata={})
    assert 'task_template_id' in b and 'skill_name' in b
    g = build_task_prompt(task_id='t5', task_type='unknown_x', input_payload={}, metadata={})
    assert 'Generic task' in g


def test_agent_async_task_prompt_drives_background_skill_execution():
    p = build_task_prompt(
        task_id='t-agent-1',
        task_type='agent_async_task',
        input_payload={
            'user_task': 'Analyze the checkout flow and propose fixes.',
            'skill_name': 'runtime-review',
            'task_session_id': 'agent-task:root-1',
            'root_task_id': 'root-1',
        },
        metadata={},
    )
    assert 'background task' in p
    assert 'autonomous' in p.lower()
    assert 'runtime-review' in p
    assert 'Analyze the checkout flow and propose fixes.' in p
    assert 'final_response' in p
    assert 'needs_user_input' in p
    assert 'Return exactly one JSON object' in p


def test_task_prompt_sanitizes_metadata_authorization_allowlists():
    metadata = {
        'portal_delegation_source': 'jira',
        'portal_delegation_provider': 'atlassian',
        'portal_task_session_id': 'agent-task:root-1',
        'authorization_source': 'portal-debug',
        'allowed_external_systems': ['github'],
        'allowed_actions': ['review_pull_request'],
        'allowed_adapter_actions': ['adapter:github:review_pull_request'],
        'allowed_capability_ids': ['capability.github.review'],
        'allowed_capability_types': ['tool'],
        'resolved_action_mappings': {'review_pull_request': 'adapter:github:review_pull_request'},
        'unresolved_tools': ['jira-transition'],
        'unresolved_skills': ['jira-review'],
        'unresolved_channels': ['jira'],
        'unresolved_actions': ['transition_issue'],
        'skill_details': {'name': 'github-review'},
    }
    original = copy.deepcopy(metadata)

    p = build_task_prompt(
        task_id='t-agent-jira-1',
        task_type='agent_async_task',
        input_payload={
            'schema': 'agent_async_task.v1',
            'user_task': 'Review the Jira delegation and report blockers.',
        },
        metadata=metadata,
    )

    for forbidden in (
        'authorization_source',
        'allowed_external_systems',
        'allowed_actions',
        'allowed_adapter_actions',
        'allowed_capability_ids',
        'allowed_capability_types',
        'resolved_action_mappings',
        'unresolved_tools',
        'unresolved_skills',
        'unresolved_channels',
        'unresolved_actions',
        'skill_details',
        'review_pull_request',
        'adapter:github:review_pull_request',
    ):
        assert forbidden not in p

    assert 'portal_delegation_source' in p
    assert 'same OpenCode runtime profile/permission configuration as chat' in p
    assert 'Do not treat missing Portal allowed_* metadata as denial' in p
    assert metadata == original
