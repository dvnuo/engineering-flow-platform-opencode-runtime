#!/usr/bin/env bash
set -euo pipefail

export HOME="${HOME:-/root}"
export OPENCODE_SERVER_USERNAME="${OPENCODE_SERVER_USERNAME:-opencode}"

if [[ -z "${OPENCODE_SERVER_PASSWORD:-}" ]]; then
  OPENCODE_SERVER_PASSWORD="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)"
  export OPENCODE_SERVER_PASSWORD
  echo "OPENCODE_SERVER_PASSWORD was not set; generated an internal password for adapter-to-opencode communication."
fi

export OPENCODE_CONFIG="${OPENCODE_CONFIG:-/workspace/.opencode/opencode.json}"
export EFP_RUNTIME_TYPE="${EFP_RUNTIME_TYPE:-opencode}"
export EFP_SKILLS_DIR="${EFP_SKILLS_DIR:-/app/skills}"
export EFP_TOOLS_DIR="${EFP_TOOLS_DIR:-/app/tools}"
export EFP_WORKSPACE_DIR="${EFP_WORKSPACE_DIR:-/workspace}"
export EFP_ADAPTER_STATE_DIR="${EFP_ADAPTER_STATE_DIR:-/root/.local/share/efp-compat}"
export OPENCODE_DATA_DIR="${OPENCODE_DATA_DIR:-/root/.local/share/opencode}"
export EFP_OPENCODE_URL="${EFP_OPENCODE_URL:-http://127.0.0.1:4096}"
export PYTHONPATH="${EFP_TOOLS_DIR}/python:${PYTHONPATH:-}"
OPENCODE_LOG_FILE="${OPENCODE_LOG_FILE:-/tmp/efp-opencode-serve.log}"
: > "${OPENCODE_LOG_FILE}"

echo "Initializing EFP OpenCode runtime assets..."
python -m efp_opencode_adapter.init_assets

echo "Ensuring OpenCode custom tool dependencies..."
python -m efp_opencode_adapter.tool_deps \
  --workspace-dir "${EFP_WORKSPACE_DIR}" \
  --vendored-dir "${EFP_OPENCODE_TOOL_DEPS_DIR:-/opt/opencode-tool-deps}" \
  --opencode-version "${OPENCODE_VERSION:-}"

echo "Runtime package versions:"
python3 - <<'PY'
import json, pathlib
p = pathlib.Path("/app/runtime/package.json")
if p.exists():
    data = json.loads(p.read_text())
    deps = data.get("dependencies", {})
    print(json.dumps({"opencode-ai": deps.get("opencode-ai"), "@opencode-ai/plugin": deps.get("@opencode-ai/plugin")}, sort_keys=True))
PY

echo "opencode version $(opencode --version)"
echo "Starting opencode serve on 127.0.0.1:4096..."
opencode serve --hostname 127.0.0.1 --port 4096 > >(tee -a "${OPENCODE_LOG_FILE}") 2>&1 &
OPENCODE_PID=$!
echo "opencode serve listening on 127.0.0.1:4096"

cleanup() {
  echo "Stopping EFP OpenCode runtime..."
  if [[ -n "${ADAPTER_PID:-}" ]]; then
    kill "${ADAPTER_PID}" 2>/dev/null || true
    wait "${ADAPTER_PID}" 2>/dev/null || true
  fi
  if [[ -n "${OPENCODE_PID:-}" ]]; then
    kill "${OPENCODE_PID}" 2>/dev/null || true
    wait "${OPENCODE_PID}" 2>/dev/null || true
  fi
}

trap cleanup SIGTERM SIGINT EXIT

dump_startup_diagnostics() {
  echo "OpenCode startup diagnostics:" >&2
  echo "OPENCODE_VERSION=${OPENCODE_VERSION:-}" >&2
  echo "opencode binary version: $(opencode --version 2>/dev/null || true)" >&2
  echo "OpenCode serve log tail:" >&2
  tail -200 "${OPENCODE_LOG_FILE}" >&2 || true
  python -m efp_opencode_adapter.tool_registry_diagnostics \
    --workspace-dir "${EFP_WORKSPACE_DIR}" \
    --opencode-url "${EFP_OPENCODE_URL}" \
    --timeout "${EFP_OPENCODE_DIAGNOSTICS_TIMEOUT_SECONDS:-10}" >&2 || true
}

python -m efp_opencode_adapter.health --wait --timeout "${EFP_OPENCODE_READY_TIMEOUT_SECONDS:-60}"

echo "Checking OpenCode ToolRegistry readiness..."
if ! python -m efp_opencode_adapter.tool_registry_check \
  --timeout "${EFP_OPENCODE_TOOL_REGISTRY_TIMEOUT_SECONDS:-600}" \
  --request-timeout "${EFP_OPENCODE_TOOL_REGISTRY_REQUEST_TIMEOUT_SECONDS:-600}"
then
  dump_startup_diagnostics
  exit 1
fi

echo "Starting efp-opencode-adapter on 0.0.0.0:8000..."
python -m efp_opencode_adapter.server --host 0.0.0.0 --port 8000 --opencode-url "${EFP_OPENCODE_URL}" &
ADAPTER_PID=$!
echo "adapter listening on 0.0.0.0:8000"

wait "${ADAPTER_PID}"
