# engineering-flow-platform-opencode-runtime

This repository contains the **T05 + T06 + T07 + T08 + T09 + T10 + T11 scaffold** for an EFP-compatible OpenCode runtime adapter image.

## Runtime topology

- Portal-facing runtime endpoint: `0.0.0.0:8000`
- Internal native OpenCode server: `127.0.0.1:4096`
- Portal **must not** call OpenCode native API directly.
- OpenCode version is pinned to **`1.14.29`**.

## T05 + T06 + T07 + T08 + T09 + T10 + T11 scope

Implemented in T05:

- `GET /health`
- `GET /actuator/health`
- `POST /api/internal/runtime-profile/apply`
- `GET /api/capabilities`
- asset initialization scaffold (`python -m efp_opencode_adapter.init_assets`)
- OpenCode readiness check via `/global/health`
- opencode.json generation with strict permission baseline
- provider/model mapping to `provider/model`
- capability catalog from builtins/tools/skills/agents/MCP

Implemented in T06:

- `POST /api/chat`
- `POST /api/chat/stream` (SSE compatibility mode)
- `GET /api/events` (WebSocket compatibility mode)
- `GET /api/sessions`
- `GET /api/sessions/{session_id}`
- `GET /api/sessions/{session_id}/chatlog`
- `POST /api/sessions/{session_id}/rename`
- `DELETE /api/sessions/{session_id}`
- `POST /api/clear`
- compatibility stubs for message mutation endpoints (HTTP 501)

Implemented in T08:

- EFP skills converter:
  `/app/skills/<skill>/skill.md` -> `/workspace/.opencode/skills/<normalized-name>/SKILL.md`
- skills-index.json:
  `$EFP_ADAPTER_STATE_DIR/skills-index.json`
- optional generated subagent prompts:
  `/workspace/.opencode/agents/skill-<name>.md`


Implemented in T09:

- /app/tools ToolDescriptor -> /workspace/.opencode/tools/efp_*.ts
- Python runner bridge through python -m efp_tools.runner
- tools-index.json at $EFP_ADAPTER_STATE_DIR/tools-index.json
- entrypoint PYTHONPATH integration for /app/tools/python

T10 implemented:

- `/api/server-files` browse/read/content/upload/delete/download
- legacy `GET /api/files` and `GET /api/files/read` aliases
- attachment upload/parse/list/preview/download/delete
- `/api/context/files` and `/api/chunks/search`
- `build_attachment_context` helper for T06 integration

T11 implemented:

- POST /api/tasks/execute
- GET /api/tasks/{task_id}
- persisted task state at $EFP_ADAPTER_STATE_DIR/tasks/{task_id}.json
- task prompt templates for GitHub PR review, Jira workflow review, delegation, bundle, generic
- task lifecycle events on /api/events

Not implemented in this task:
- complex parsers for PDF/DOCX/XLSX (returns `unsupported_file_type` in MVP)

## Security defaults

Generated minimal `opencode.json` defaults include:

- `autoupdate: false`
- `share: "disabled"`
- `permission["*"] = "ask"`
- dangerous bash patterns denied (`rm`, `sudo`, `git push`, `curl | bash`)
- `external_directory: "deny"`
- runtime runs as non-root UID/GID `10001`

## Local development

```bash
python -m pytest -q
python -m efp_opencode_adapter.init_assets

# T08 skill sync smoke
python -m efp_opencode_adapter.skill_sync \
  --skills-dir /app/skills \
  --opencode-skills-dir /workspace/.opencode/skills \
  --state-dir /home/opencode/.local/share/efp-compat

# T09 tool sync smoke
python -m efp_opencode_adapter.tool_sync \
  --tools-dir /app/tools \
  --opencode-tools-dir /workspace/.opencode/tools \
  --state-dir /home/opencode/.local/share/efp-compat

```

## Docker

Build and run:

```bash
docker build -t efp-opencode-runtime:test .
docker run --rm -p 8000:8000 -e OPENCODE_SERVER_PASSWORD=test-password efp-opencode-runtime:test
curl http://localhost:8000/health
```

Automated smoke validation:

```bash
bash scripts/smoke.sh
```

`bash scripts/smoke.sh` will:

- build the image
- run the container with only `8000` mapped (no `4096` host mapping)
- verify `/health` returns `status=ok`, `engine=opencode`, `opencode_version=1.14.29`
- verify container runtime UID is `10001`
- verify generated `/workspace/.opencode/opencode.json` security defaults (`autoupdate=false`, `share=disabled`, `permission["*"]="ask"`, `external_directory="deny"`)

Version verification:

```bash
opencode --version
```

Expected output contains `1.14.29`.

Package lock verification:

```bash
node -e "const p=require('./package-lock.json'); console.log(p.packages?.['node_modules/opencode-ai']?.version || p.dependencies?.['opencode-ai']?.version)"
```

Expected output:

```text
1.14.29
```

