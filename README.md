# engineering-flow-platform-opencode-runtime

This repository contains the **T05 scaffold** for an EFP-compatible OpenCode runtime adapter image.

## Runtime topology

- Portal-facing runtime endpoint: `0.0.0.0:8000`
- Internal native OpenCode server: `127.0.0.1:4096`
- Portal **must not** call OpenCode native API directly.
- OpenCode version is pinned to **`1.14.29`**.

## T05 + T06 + T08 scope

Implemented in T05:

- `GET /health`
- `GET /actuator/health`
- asset initialization scaffold (`python -m efp_opencode_adapter.init_assets`)
- OpenCode readiness check via `/global/health`

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

Not implemented in this task:

- `/api/tasks`
- full runtime profile mapping/apply policy
- tools wrapper
- files / attachments / context integrations

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

