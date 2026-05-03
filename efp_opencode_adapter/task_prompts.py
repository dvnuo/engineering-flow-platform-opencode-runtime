from __future__ import annotations

import json
from typing import Any


def _base(task_id: str, task_type: str, input_payload: dict[str, Any], metadata: dict[str, Any], source: str | None, shared_context_ref: str | None, context_ref: Any) -> str:
    return (
        "This is an EFP Portal automation task, not a normal chat.\n"
        "Do not write back to GitHub/Jira/Confluence/Slack unless metadata/policy explicitly allows mutation tools.\n"
        "If execution_mode=chat_tool_loop, read/analyze tools may be used when explicitly allowed; mutation/writeback actions still require explicit policy permission.\n"
        "If data is missing, do not invent it; return status=blocked and list missing fields.\n"
        "Do not include secrets/tokens/raw credentials in output JSON.\n"
        "If policy/permission blocks execution, return status=blocked.\n\n"
        f"task_id: {task_id}\n"
        f"task_type: {task_type}\n"
        f"source: {source}\n"
        f"shared_context_ref: {shared_context_ref}\n"
        f"context_ref: {json.dumps(context_ref, ensure_ascii=False)}\n"
        f"metadata: {json.dumps(metadata, ensure_ascii=False)}\n"
        f"input_payload: {json.dumps(input_payload, ensure_ascii=False)}\n\n"
        "Return exactly one JSON object. Do not wrap it in markdown unless your runtime requires it.\n"
        "The JSON object must match:\n"
        '{"status":"success|error|blocked","summary":"...","artifacts":[],"blockers":[],"next_recommendation":"...","audit_trace":[],"external_actions":[]}\n'
    )


def build_task_prompt(*, task_id: str, task_type: str, input_payload: dict[str, Any], metadata: dict[str, Any], source: str | None = None, shared_context_ref: str | None = None, context_ref: Any = None) -> str:
    prompt = _base(task_id, task_type, input_payload, metadata, source, shared_context_ref, context_ref)
    if task_type in {"github_review_task", "github_pr_review"}:
        return prompt + (
            "GitHub PR review task fields: owner, repo, pull_number, head_sha, base_sha, review_request_id, review_target, review_target_type, writeback_mode, allowed tools/actions, metadata.portal_head_sha.\n"
            "Do not write back to GitHub unless Portal policy explicitly allows mutation tools. Missing data must return status=blocked.\n"
            'If the current PR head SHA differs from metadata.portal_head_sha or input_payload.head_sha, return: {"status":"error","summary":"PR head SHA changed before review completed.","error_code":"superseded_by_new_head_sha","recommendation":"comment","review_comments":[],"artifacts":[],"blockers":["PR head SHA changed"],"next_recommendation":"Re-dispatch review for the latest head SHA.","audit_trace":[],"external_actions":[]}\n'
            'Also keep output fields: {"summary":"...","recommendation":"comment|approve|request_changes","review_comments":[],"error_code":null}.\n'
        )
    if task_type in {"jira_workflow_review_task", "jira_workflow_review"}:
        return prompt + (
            "Jira workflow review fields: issue_key, project_key, workflow_rule_id, portal_workflow_rule_id, transition config, reassign config, review criteria, allowed transition/reassign/writeback actions.\n"
            "Do not transition or reassign Jira issues unless Portal policy explicitly allows the specific mutation action. Missing data must return status=blocked.\n"
        )
    if task_type == "delegation_task":
        return prompt + (
            "Delegation fields: group_id, leader_agent_id, assignee_agent_id, coordination_run_id, objective, scoped_context_ref, expected_output_schema, visibility, reply_target_type, round_index, agent_mode, input_artifacts, context_ref.\n"
            "Return a top-level JSON object for this delegated work. The adapter will wrap it into Portal output_payload.delegation_result; do not return the whole public runtime response unless explicitly asked.\n"
            "Required fields: status, summary, artifacts, blockers, next_recommendation, audit_trace.\n"
        )
    if task_type in {"bundle_action_task", "requirement_bundle_analysis"}:
        return prompt + (
            "Bundle task fields: task_template_id, metadata.portal_task_template_id, skill_name, bundle_id, sources, input_payload, shared_context_ref, context_ref.\n"
            "Do not perform mutation/writeback unless explicitly allowed by policy. Missing data must return status=blocked.\n"
            'Also keep output fields: {"summary":"...","artifacts":[],"bundle_updates":[],"error_code":null}.\n'
        )
    if task_type == "triggered_event_task":
        return prompt + (
            "Triggered event fields: source_kind, portal_binding_id, portal_automation_rule, portal_automation_rule_id, body, and GitHub/Jira issue/PR/comment identifiers if present.\n"
            "Do not perform mutation/writeback unless explicitly allowed by policy. Missing data must return status=blocked.\n"
        )
    return prompt + "Generic task: unknown task_type allowed; return structured JSON result.\n"
