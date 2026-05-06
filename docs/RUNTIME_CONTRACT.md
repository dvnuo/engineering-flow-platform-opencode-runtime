# Runtime Contract

Portal -> adapter `:8000` -> internal OpenCode `:4096`.

- Portal only calls adapter on `:8000`.
- OpenCode `:4096` is internal-only and must not be exposed to Portal.

Required env:
- `EFP_RUNTIME_TYPE=opencode`
- `EFP_WORKSPACE_DIR`
- `EFP_SKILLS_DIR`
- `EFP_TOOLS_DIR`
- `EFP_ADAPTER_STATE_DIR`
- `OPENCODE_DATA_DIR`
- `OPENCODE_CONFIG`
- `OPENCODE_VERSION`
- `OPENCODE_SERVER_USERNAME`
- `OPENCODE_SERVER_PASSWORD`
- `PORTAL_AGENT_ID`

State dirs:
- `/workspace`
- `/workspace/.opencode/skills`
- `/workspace/.opencode/tools`
- `/home/opencode/.local/share/opencode`
- `/home/opencode/.local/share/efp-compat`

Portal-facing endpoints include `/health`, `/actuator/health`, `/api/chat`, `/api/chat/stream`, `/api/events`, `/api/capabilities`, `/api/skills`, `/api/tasks/execute`, `/api/tasks/{task_id}`, `/api/tasks/{task_id}/cancel`, `/api/internal/runtime-profile/apply`, `/api/internal/runtime-profile/status`, `/api/sessions`, `/api/queue/status`, `/api/server-files`.

Asset mapping contract:
- EFP skill name -> OpenCode normalized skill name.
- legacy tool name -> `efp_*` OpenCode wrapper name.
- persisted indexes: `skills-index.json`, `tools-index.json`.

Live LLM contract checks are opt-in:
- `RUNTIME_CONTRACT_ENABLE_CHAT=1`
- `RUNTIME_CONTRACT_ENABLE_TASKS=1`
