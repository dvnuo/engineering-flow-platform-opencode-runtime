# Runtime Contract

## Overview
This runtime is a **runtime-only adapter contract** for Portal/EFP integration validation. It is not a full Portal or Kubernetes E2E environment.

## Runtime topology
Portal -> adapter `0.0.0.0:8000` -> internal OpenCode `127.0.0.1:4096`.

## Non-goals
- No direct Portal -> OpenCode `:4096` traffic.
- No claim that default smoke validates live LLM production behavior.

## Required environment variables
- `EFP_RUNTIME_TYPE=opencode`
- `EFP_WORKSPACE_DIR`
- `EFP_SKILLS_DIR`
- `EFP_TOOLS_DIR`
- `EFP_ADAPTER_STATE_DIR`
- `OPENCODE_DATA_DIR`
- `OPENCODE_CONFIG`
- `PORTAL_AGENT_ID`

`OPENCODE_VERSION` may be used as Docker build/config metadata, but it is not required by Portal and must not block runtime startup.

## Required mounted directories
- `/workspace`
- `/workspace/.opencode/skills`
- `/workspace/.opencode/tools`
- `/root/.local/share/opencode`
- `/root/.local/share/efp-compat`

## Portal-facing endpoints
`/health`, `/actuator/health`, `/api/chat`, `/api/chat/stream`, `/api/events`, `/api/capabilities`, `/api/skills`, `/api/tasks/execute`, `/api/tasks/{task_id}`, `/api/tasks/{task_id}/cancel`, `/api/internal/runtime-profile/apply`, `/api/internal/runtime-profile/status`, `/api/sessions`, `/api/sessions/{session_id}/messages/{message_id}/delete-from-here`, `/api/sessions/{session_id}/messages/{message_id}/edit`, `/api/queue/status`, `/api/server-files`, `/api/permissions/{permission_id}/respond`.

Message mutation keeps Portal `session_id` stable while the adapter may replace the internal OpenCode session id after fork/new-session mutation.

## Internal-only OpenCode server
Portal only calls adapter `:8000`. OpenCode `:4096` must not be exposed.
The internal OpenCode server is intentionally reachable only over loopback (`127.0.0.1`) inside the runtime container/pod; this localhost network binding is the security boundary.

## Skills asset mapping
EFP skill names are normalized for OpenCode and persisted in `skills-index.json`.

## Tools asset mapping
Legacy tool names map to `efp_*` OpenCode wrapper names and are persisted in `tools-index.json`.

## State persistence contract
`/root/.local/share/opencode` and `/root/.local/share/efp-compat` should be persistent in production. Adapter state in `EFP_ADAPTER_STATE_DIR` must persist sessions/tasks/profile overlays.

## Runtime profile apply/status contract
`/api/internal/runtime-profile/apply` and `/api/internal/runtime-profile/status` provide apply status, revision/runtime_profile_id propagation, and pending-restart visibility.

## Runtime contract tests
`runtime_contract_tests` are runtime-only checks, not Portal/K8s E2E checks.

## Live LLM checks are opt-in
- `RUNTIME_CONTRACT_ENABLE_CHAT=1`
- `RUNTIME_CONTRACT_ENABLE_TASKS=1`

## Failure modes and expected status
- opencode unavailable -> adapter returns 502 on upstream-dependent flows.
- profile pending restart -> status payload shows pending restart.
- missing skills dir -> health/capabilities surface degraded signals.
- missing tools dir -> tools sync/capability mapping warnings.
- state dir unwritable -> runtime state persistence failures.

## Permission mode contract
- `EFP_OPENCODE_PERMISSION_MODE=workspace_full_access` (default)
- `EFP_OPENCODE_ALLOW_BASH_ALL=true` (default)
- Default: built-in `edit`/`write` are `allow`, bash is `{"*":"allow"}`.
- `profile_policy` keeps legacy ask/deny semantics for backward compatibility.

## Chat final state contract
- `completed` requires visible assistant text and returns `ok=true`.
- `blocked`, `incomplete`, `error`, and `empty_final` must return `ok=false`.
- `empty_final` must include non-empty diagnostic response text.
- Runtime must not return an empty assistant response as success.

## Portal/runtime responsibility split
- Portal injects runtime env values and renders non-success chat outcomes.
- Runtime generates the OpenCode permission map and determines final completion state.
