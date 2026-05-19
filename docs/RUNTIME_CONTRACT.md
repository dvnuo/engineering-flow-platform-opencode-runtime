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

## Required mounted directories
- `/workspace`
- `/workspace/.opencode/skills`
- `/root/.local/share/opencode`
- `/root/.local/share/efp-compat`

## Portal-facing endpoints
At minimum, runtime provides:
- `/health`
- `/actuator/health`
- `/api/chat`
- `/api/chat/stream`
- `/api/sessions`
- `/api/skills`
- `/api/capabilities`
- `/api/permissions/respond`
- `/api/tasks/execute`
- `/api/queue/status`
- `/api/server-files/*`
- `/api/events/ws`

## Internal-only OpenCode server
OpenCode is an implementation detail behind adapter APIs.
- It is bound to `:4096` internal loopback.
- It must not be exposed as a direct Portal target.

## Internal-only adapter endpoints
- `POST /api/internal/skills/resync`

## Skills asset mapping
- Portal provides skills only (`EFP_SKILLS_DIR`, default `/app/skills`).
- Adapter syncs source skills into `/workspace/.opencode/skills` and writes `skills-index` state.
- Directory skill input supports `<name>/SKILL.md` (preferred) and `<name>/skill.md` (legacy-compatible). If both exist, `SKILL.md` is used.
- Top-level `*.md` files with valid frontmatter are still supported as flat prompt skills.
- Target output is always `/workspace/.opencode/skills/<normalized-name>/SKILL.md`.
- Target output never includes lowercase `/workspace/.opencode/skills/<normalized-name>/skill.md`.
- Directory skill sidecar resources are copied recursively, including `scripts/`, `templates/`, `reference/`, `examples/`, and ordinary files.
- Entry files `SKILL.md` and `skill.md` are not copied as sidecar resources.
- Sidecar resources are copied only for directory skills, not for top-level flat markdown skills.
- Dynamic skill additions or resource changes after runtime startup require `POST /api/internal/skills/resync` or a runtime restart.
- `tools` / `task_tools` in source skill frontmatter are informational metadata only.
- Source metadata is not interpreted as runtime executable wrapper mappings.

## State persistence contract
Persisted state directories:
- OpenCode runtime state: `/root/.local/share/opencode`
- Adapter state: `/root/.local/share/efp-compat`

State should survive runtime restarts when mounted persistently.

## Runtime profile apply/status contract
- Apply endpoint updates runtime profile and OpenCode config.
- Status endpoint reports apply status, revision, and restart/health-related state.
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
