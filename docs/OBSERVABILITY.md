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
`execution.started`, `assistant_delta`, `complete`, `execution.completed`, `execution.failed`.

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

## Adapter stdout logs
All adapter logging goes to STDOUT (what `kubectl logs` reads), one line per record with `key=value` fields:
- `http.start method=.. path=.. request_id=..` and `http.end method=.. path=.. status=.. duration_ms=.. request_id=..` — one pair per HTTP request, the end line always emitted (including on handler errors). `request_id` reuses an inbound `X-Request-Id`, `X-Correlation-Id` or `X-Trace-Id` header when present, otherwise a generated id.
- `opencode: <line>` — every stdout/stderr line of the managed `opencode serve` child, re-emitted through the adapter logger and secret-redacted. The child log file under the adapter state dir is still written (`/api/internal/opencode/log-tail` keeps working). Redaction is fail-closed: the effective secret values from the profile projection env handed to the child (not the adapter's own `os.environ`, which no longer holds them) plus credential *shapes* — `ghp_/gho_/ghu_/ghs_/ghr_/github_pat_`, `sk-`, Atlassian `ATATT`/`ATCTT`, AWS key ids and secret access keys, `Bearer`/`Basic` values, URL userinfo, and generic `password=`/`token=`/`authorization:` pairs. Key/value redaction matches *whole* key names, so token-accounting telemetry (`tokens=`, `input_tokens=`, `output_tokens=`, `tokenizer=`, `token_count=`) and non-secret values (`secret=false`) are left intact and JSON stays parseable.
- On shutdown the child's output is drained until it stops making progress (not a fixed wall-clock budget), so a burst that ends in a fatal stack trace reaches stdout, the log file and `last_startup_error` in full. Once the child process itself has exited the drain is additionally capped at a few seconds: a tool subprocess that inherited the merged pipe and outlived `opencode serve` must not hold `stop()`/`restart()` open while opencode is down.
- The pump yields to the event loop while draining a burst, so relayed build output (`npm test`, `mvn verify`) cannot stall `/health`, `/ready` or SSE delivery.
- `opencode.process.started pid=.. reason=.. log_file=..`, `opencode.process.output_closed pid=.. returncode=..`, `opencode.log.line_dropped pid=.. reason=line_too_long`.

Level precedence: `EFP_LOG_LEVEL`, then `LOG_LEVEL`, then `EFP_DEBUG=1` => DEBUG, else INFO. The runtime profile's `debug` section is projected into those variables and re-applied to the adapter's own logger right after the boot projection.

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
