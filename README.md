# engineering-flow-platform-opencode-runtime

This repository contains the **T05 scaffold** for an EFP-compatible OpenCode runtime adapter image.

## Runtime topology

- Portal-facing runtime endpoint: `0.0.0.0:8000`
- Internal native OpenCode server: `127.0.0.1:4096`
- Portal **must not** call OpenCode native API directly.
- OpenCode version is pinned to **`1.14.29`**.

## T05 scope

Implemented in this task:

- `GET /health`
- `GET /actuator/health`
- asset initialization scaffold (`python -m efp_opencode_adapter.init_assets`)
- OpenCode readiness check via `/global/health`

Not implemented in this task:

- `/api/chat`
- `/api/chat/stream`
- `/api/tasks`
- runtime profile mapping
- tools wrapper
- skills converter
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
