from efp_opencode_adapter.task_completion_parser import parse_task_completion


def test_parser_modes_and_defaults():
    s, p, _ = parse_task_completion('{"status":"success","summary":"done"}', task_type='generic_agent_task')
    assert s == 'success' and p['summary'] == 'done'
    assert p['blockers'] == []
    assert p['audit_trace'] == []
    assert p['external_actions'] == []

    s, p, _ = parse_task_completion('```json\n{"status":"success","summary":"done2"}\n```', task_type='generic_agent_task')
    assert p['summary'] == 'done2'

    s, p, _ = parse_task_completion('not json', task_type='generic_agent_task')
    assert s == 'success' and p['raw_text']

    s, _, _ = parse_task_completion('waiting for permission approval required', task_type='generic_agent_task')
    assert s == 'blocked'

    s, p, _ = parse_task_completion('{"status":"error","nested":{"error_code":"superseded_by_new_head_sha"}}', task_type='github_review_task')
    assert s == 'error'
    assert p['error_code'] == 'superseded_by_new_head_sha'


def test_delegation_nested_extraction():
    s, p, _ = parse_task_completion('{"status":"success","summary":"ok"}', task_type='delegation_task')
    assert isinstance(p['delegation_result'], dict)

    s, p, _ = parse_task_completion(
        '{"status":"success","output_payload":{"delegation_result":{"status":"done","summary":"nested","artifacts":[{"x":1}],"blockers":[]}}}',
        task_type='delegation_task',
    )
    assert p['delegation_result']['summary'] == 'nested'
    assert p['delegation_result']['artifacts'] == [{'x': 1}]
