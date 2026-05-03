from efp_opencode_adapter.task_prompts import build_task_prompt


def test_github_prompt_and_alias():
    p = build_task_prompt(task_id='t1', task_type='github_review_task', input_payload={'repo': 'x', 'pull_number': 1, 'head_sha': 'h'}, metadata={'portal_head_sha': 'h'})
    assert 'superseded_by_new_head_sha' in p
    assert 'Do not write back to GitHub' in p
    p2 = build_task_prompt(task_id='t1', task_type='github_pr_review', input_payload={}, metadata={})
    assert 'GitHub PR review task fields' in p2


def test_other_prompts():
    p = build_task_prompt(task_id='t2', task_type='jira_workflow_review_task', input_payload={'issue_key': 'ABC-1'}, metadata={'workflow_rule_id': 'w'})
    assert 'issue_key' in p and 'Do not transition or reassign Jira issues' in p
    d = build_task_prompt(task_id='t3', task_type='delegation_task', input_payload={'group_id': 'g'}, metadata={})
    assert 'leader_agent_id' in d and 'expected_output_schema' in d
    b = build_task_prompt(task_id='t4', task_type='bundle_action_task', input_payload={}, metadata={})
    assert 'task_template_id' in b and 'skill_name' in b
    g = build_task_prompt(task_id='t5', task_type='unknown_x', input_payload={}, metadata={})
    assert 'Generic task' in g
