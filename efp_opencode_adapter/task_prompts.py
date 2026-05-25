from __future__ import annotations

import json
from typing import Any


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _agent_async_task_prompt(task_id: str, input_payload: dict[str, Any], metadata: dict[str, Any]) -> str:
    skill_name = _first_text(input_payload.get("skill_name"), metadata.get("portal_skill_name"))
    task_text = _first_text(input_payload.get("user_task"), input_payload.get("followup_task"))
    task_session_id = _first_text(input_payload.get("task_session_id"), metadata.get("portal_task_session_id"))
    root_task_id = _first_text(input_payload.get("root_task_id"), metadata.get("portal_root_task_id"))
    parent_task_id = _first_text(input_payload.get("parent_task_id"), metadata.get("portal_parent_task_id"))
    autonomous_instruction = _first_text(input_payload.get("autonomous_instruction"))
    skill_ref = skill_name or "(none provided)"

    return (
        "Agent async task instructions:\n"
        "- This is an EFP Portal background task launched from Portal Tasks, not interactive chat.\n"
        "- Work autonomously as a long-running background agent task.\n"
        f"- Selected skill: `{skill_ref}`.\n"
        f"- Use the native OpenCode `skill` tool to load skill `{skill_ref}` if available.\n"
        f"- Follow `.opencode/skills/{skill_ref}/SKILL.md` when that skill exists.\n"
        "- Do not claim the selected skill is running unless you have actually loaded and applied it.\n"
        "- If the selected skill cannot be loaded, continue with useful best-effort work unless the skill is required; record exact blockers in JSON.\n\n"
        "Task identifiers:\n"
        f"- task_id: {task_id}\n"
        f"- task_session_id: {task_session_id or '(not provided)'}\n"
        f"- root_task_id: {root_task_id or '(not provided)'}\n"
        f"- parent_task_id: {parent_task_id or '(none)'}\n\n"
        "User task content:\n"
        f"{task_text or '(no user_task or followup_task provided)'}\n\n"
        "Autonomous execution rules:\n"
        "- Do not ask the user questions during execution.\n"
        "- Make reasonable assumptions and proceed independently.\n"
        "- Complete as much of the task as possible with available context and tools.\n"
        "- If information is truly insufficient, return status \"blocked\" with minimal missing information in blockers and needs_user_input true.\n"
        "- Preserve secrets: do not output tokens, credentials, API keys, or raw authorization values.\n"
        f"{('- Portal autonomous instruction: ' + autonomous_instruction + chr(10)) if autonomous_instruction else ''}"
        "\nReturn exactly one JSON object. Do not wrap it in markdown.\n"
        "The JSON object must match this schema:\n"
        "{\n"
        '  "status": "success|blocked|error",\n'
        '  "summary": "...",\n'
        '  "final_response": "...",\n'
        '  "needs_user_input": false,\n'
        '  "blockers": [],\n'
        '  "next_recommendation": "...",\n'
        '  "artifacts": [],\n'
        '  "audit_trace": [],\n'
        '  "external_actions": []\n'
        "}\n"
    )


def _base(task_id: str, task_type: str, input_payload: dict[str, Any], metadata: dict[str, Any], source: str | None, shared_context_ref: str | None, context_ref: Any, *, include_default_schema: bool = True) -> str:
    prompt = (
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
    )
    if not include_default_schema:
        return prompt
    return prompt + (
        "Return exactly one JSON object. Do not wrap it in markdown unless your runtime requires it.\n"
        "The JSON object must match:\n"
        '{"status":"success|error|blocked","summary":"...","artifacts":[],"blockers":[],"next_recommendation":"...","audit_trace":[],"external_actions":[]}\n'
    )


def build_task_prompt(*, task_id: str, task_type: str, input_payload: dict[str, Any], metadata: dict[str, Any], source: str | None = None, shared_context_ref: str | None = None, context_ref: Any = None) -> str:
    prompt = _base(task_id, task_type, input_payload, metadata, source, shared_context_ref, context_ref, include_default_schema=task_type != "agent_async_task")
    if task_type == "agent_async_task":
        return prompt + _agent_async_task_prompt(task_id, input_payload, metadata)
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
