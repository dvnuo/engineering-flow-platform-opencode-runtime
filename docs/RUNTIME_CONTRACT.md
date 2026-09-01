# Runtime Contract

## Overview
This repository provides an OpenCode-based runtime adapter for EFP-facing APIs.

- Portal only calls adapter APIs on port `:8000`.
- OpenCode runs as an internal dependency on `:4096` and must not be exposed externally.
- External tools subsystem removed / not supported.
- Portal provides skills only; runtime maps skills into `/workspace/.opencode/skills` and runtime state.

## Runtime topology
- Adapter service: `0.0.0.0:8000` (Portal-facing).
- Internal OpenCode server: `127.0.0.1:4096` (runtime-only).
- Runtime workspace root: `/workspace`.

## Non-goals
- No EFP external tools subsystem.
- No external tool wrappers generated from manifests.
- No tools index contract.
- No wrapper mapping contract from source skill metadata.

## Required environment variables
- `EFP_RUNTIME_TYPE=opencode`
- `EFP_WORKSPACE_DIR`
- `EFP_SKILLS_DIR`
- `EFP_ADAPTER_STATE_DIR`
- `OPENCODE_DATA_DIR`
- `OPENCODE_CONFIG`
- `EFP_PROFILE_CONFIG` (rendered profile payload JSON from the per-profile Secret)
- `EFP_PROFILE_REVISION` (profile revision string from the Secret)
- `EFP_PROFILE_ID` (profile id, or `none` for unbound agents)

## Required mounted directories
- `/workspace`
- `/workspace/.opencode/skills`
- `/root/.local/share/opencode`
- `/root/.local/share/efp-compat`

## Portal-facing endpoints
At minimum, runtime provides:
- `/health`
- `/actuator/health`
- `/ready`
- `/api/chat`
- `/api/chat/stream`
- `/api/sessions`
- `/api/skills`
- `/api/capabilities`
- `/api/permissions/respond`
- `/api/personalization`
- `/api/sessions/{session_id}/pending-input`
- `/api/sessions/{session_id}/permission/respond`
- `/api/tasks/execute`
- `/api/queue/status`
- `/api/server-files/*`
- `/api/events/ws`

## Assistant personalization
`/api/personalization` serves the greeting and starter cards from the agents
repository branch this assistant booted with. Portal's init container copies the
repo's optional `portal/` directory into the workspace alongside `AGENTS.md` and
`instructions/`, so the runtime reads what is already on disk rather than having
Portal clone the repo a second time.

- `portal/welcome.md` -- greeting shown when a chat has no messages yet.
- `portal/cards.yaml` -- starter prompts offered as clickable cards.

Both files are optional. A branch without them returns
`{"welcome": null, "cards": []}` and Portal falls back to its generic welcome.

## Interactive input contract
Portal drives its approval card off the session rather than the permission id:
it polls `/api/sessions/{session_id}/pending-input` to rebuild a card after a
refresh, and posts the answer to
`/api/sessions/{session_id}/permission/respond` with
`{"request_id": ..., "decision": "approve"|"deny", "always": bool}`.

OpenCode keeps no record of an unanswered permission -- `GET /session/{id}`
does not report one, and the only trace is the event that announced it. The
adapter therefore reconstructs pending state from the event stream, and
`permission.requested` events carry an assembled `data.permission_request`
object for the live card.

`/api/sessions/{session_id}/question/respond` answers `501
question_response_unsupported`. OpenCode's server exposes
`/session/{id}/permissions/{permissionID}` and no question route, so a typed
answer cannot be delivered back to a `question` tool call on this runtime;
`pending-input` accordingly always reports `question_request: null`. Assistants
that depend on the question tool belong on the native runtime.

## Internal-only OpenCode server
OpenCode is an implementation detail behind adapter APIs.
- It is bound to `:4096` internal loopback.
- It must not be exposed as a direct Portal target.

## Skills asset mapping
- Portal provides skills only (`EFP_SKILLS_DIR`, default `/app/skills`).
- Adapter syncs source skills into `/workspace/.opencode/skills` and writes `skills-index` state.
- Source directory skills may use `<skill-name>/SKILL.md` (preferred) or legacy `<skill-name>/skill.md`.
- Top-level Markdown skills with frontmatter remain supported.
- OpenCode output always uses `/workspace/.opencode/skills/<normalized-name>/SKILL.md`.
- Directory skill sidecar resources are recursively copied into the same output skill directory, including `scripts/`, `templates/`, `reference/`, `examples/`, and other regular files.
- Source entry files are not copied as resources, cache directories are skipped, and symlinks are skipped.
- Skills are synced during asset initialization and before managed OpenCode startup or restart.
- `tools` / `task_tools` in source skill frontmatter are informational metadata only.
- Source metadata is not interpreted as runtime executable wrapper mappings.

## State persistence contract
Persisted state directories:
- OpenCode runtime state: `/root/.local/share/opencode`
- Adapter state: `/root/.local/share/efp-compat`

State should survive runtime restarts when mounted persistently.

## Task restart contract
The adapter follows upstream OpenCode's recovery boundary: durable
session/history state may be reused, but in-flight provider/tool activity from a
previous process is not automatically replayed on adapter startup. Active task
records found during startup are marked `blocked` with
`adapter_restarted_task_recovery_required`; callers should re-dispatch the task
when the work is still required.

Task state loading and persistence are bounded:
- `EFP_OPENCODE_TASKS_LIST_MAX_RECORDS` limits task records returned by store
  list operations. The default is `512`.
- `EFP_OPENCODE_TASKS_SCAN_MAX_RECORDS` limits candidate task files scanned
  while listing. The default is `1024`.
- `EFP_OPENCODE_TASKS_LOAD_MAX_FILE_BYTES` skips individual task files larger
  than the configured byte limit. The default is `2000000`.
- `EFP_OPENCODE_TASKS_PERSIST_MAX_FILE_BYTES` caps each persisted task record.
  Large payloads are replaced with a diagnostic omission marker while identity,
  status, error, and bounded event context remain available. The default is
  `2000000`.
- `EFP_OPENCODE_TASKS_PERSIST_EVENT_TAIL` limits runtime events retained when a
  task record must be minimized. The default is `50`.

## Runtime profile boot/status contract
Profile config is delivered exclusively through pod env at container start;
there is no apply endpoint and no hot-apply path.

- Delivery: the Portal renders each profile into a per-profile Secret and
  injects it as pod env — `EFP_PROFILE_CONFIG` (full payload JSON, key
  `opencode.json`), `EFP_PROFILE_REVISION`, and `EFP_PROFILE_ID`.
- Boot projection: the adapter parses `EFP_PROFILE_CONFIG` once at startup and
  projects it into runtime assets (opencode.json, auth.json, opencode.env,
  git/gh auth assets, atlassian/mobile CLI config, AWS auth), then removes the
  blob from its process env before the managed OpenCode child starts. The
  child env never contains `EFP_PROFILE_CONFIG`.
- Failure semantics: a missing `EFP_PROFILE_CONFIG` env var is a fatal pod
  misconfiguration (the adapter stays alive but unready); an empty
  `"config": {}` payload is a valid empty profile (base config).
- Activation is restart-only: config changes reach a pod only via a
  Portal-triggered restart with an updated Secret. The managed OpenCode
  watchdog only revives the child with the boot-time env.
- Readiness: `GET /ready` returns 200 with
  `{"ready": true, "runtime_profile_id": ..., "revision": ...}` only after the
  boot projection succeeded and the managed OpenCode child is healthy;
  otherwise 503 with `{"ready": false, "error": ...}`.
- Status endpoint reports the running revision from the pod env plus the boot
  projection record (warnings, hashes, per-integration configured flags).
- Effective config endpoint exposes sanitized runtime configuration and integration status.

## Runtime contract tests
Quick contract check:

```bash
python -m pytest -q runtime_contract_tests
```

Live runtime check against a running adapter:

```bash
RUNTIME_BASE_URL=http://localhost:8000 python -m pytest -q runtime_contract_tests
```

## Live LLM checks are opt-in
Optional live checks are guarded and skipped by default unless explicitly enabled:
- `RUNTIME_CONTRACT_ENABLE_CHAT=1`
- `RUNTIME_CONTRACT_ENABLE_TASKS=1`

## Failure modes and expected status
- Health degradation returns non-200 when internal OpenCode or state readiness fails.
- Optional live checks skip when required env is not present.
- Task polling may return transient states (`accepted`, `running`) before terminal states.
- Chat/tool execution can return empty final payload; callers should handle `empty_final` and `ok=false` states gracefully.
