# Observability

RuntimeEvent common fields: `type/event_type`, `engine`, `runtime_type`, `trace_id`, `agent_id`, `session_id`, `request_id`, `task_id`, `opencode_session_id`, `group_id`, `coordination_run_id`, `tool/tool_name`, `tool_source`, `skill_name`, `profile_version`, `runtime_profile_id`, `state/status`, `created_at/ts`, `data.trace_context`.

trace_id order: `request_id` -> `task_id` -> `session_id` -> `opencode_session_id` -> empty.

Secret redaction is always applied for secret/token/password/api_key-like values before publishing events.

EventBus filter keys: `session_id`, `task_id`, `request_id`, `agent_id`, `group_id`, `coordination_run_id`.

OpenCode raw normalization includes: `permission_request`, `permission_resolved`, `tool.started`, `tool.completed`, `tool.failed`, `assistant_delta`, `message.completed`, `session.updated`.

Portal can subscribe by `session_id` / `task_id` / `request_id` / `agent_id`.

Token-level delta is not guaranteed; adapter forwards delta only when OpenCode emits it.
