# Observability

## RuntimeEvent schema
Runtime events expose stable cross-layer fields: `type/event_type`, `engine`, `runtime_type`, `agent_id`, `session_id`, `request_id`, `task_id`, `group_id`, `coordination_run_id`, `trace_id`, `created_at/ts`, plus event-specific fields.

## trace_context schema
`data.trace_context` and top-level `trace_context` include: engine/runtime_type/agent_id/request_id/session_id/task_id/opencode_session_id/tool_name/tool_source/skill_name/profile_version/runtime_profile_id/group_id/coordination_run_id/model/provider/trace_id.

## trace_id precedence
`request_id` -> `task_id` -> `session_id` -> `opencode_session_id` -> empty string.

## EventBus filter keys
`session_id`, `task_id`, `request_id`, `agent_id`, `group_id`, `coordination_run_id`.

## Chat events
`execution.started`, `llm_thinking`, `assistant_delta`, `complete`, `execution.completed`, `execution.failed`.

## Task events
`task.accepted`, `task.started`, `task.completed`, `task.cancelled` and normalized tool/permission events mapped to task context.

## Permission events
`permission_request` and `permission_resolved` include trace context and request/session propagation.

## Tool events
`tool.started`, `tool.completed`, `tool.failed` include `tool_name` and `tool_source` (`tools_repo`, `opencode_builtin`, `unknown`).

## OpenCode raw event normalization
Includes permission/tool/message/session raw events normalization to stable adapter event types.

## Secret redaction rules
Secret/token/password/api_key-like values are sanitized before emission to top-level fields and `data.trace_context`.

## Portal subscription guidance
Portal can subscribe via `/api/events` using session/task/request/agent and correlate streams via `trace_id`.

## Limitations
- Token-level delta depends on OpenCode raw events.
- Not all OpenCode internals guarantee stable event shapes.

## JSON example
```json
{
  "type": "tool.started",
  "engine": "opencode",
  "runtime_type": "opencode",
  "agent_id": "agent-1",
  "session_id": "sess-1",
  "request_id": "req-1",
  "task_id": "task-1",
  "tool_name": "efp_context_echo",
  "tool_source": "tools_repo",
  "trace_id": "req-1",
  "data": {
    "trace_context": {"trace_id": "req-1"}
  }
}
```
