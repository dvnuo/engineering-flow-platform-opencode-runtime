# Runtime Contract

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

## Notes
- External tools subsystem removed / not supported.
- Portal-facing APIs remain chat/session/skills/capabilities/permissions/events.
