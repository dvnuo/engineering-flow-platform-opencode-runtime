#!/usr/bin/env bash
set -euo pipefail

export HOME="${HOME:-/root}"
export OPENCODE_CONFIG="${OPENCODE_CONFIG:-/workspace/.opencode/opencode.json}"
export EFP_RUNTIME_TYPE="${EFP_RUNTIME_TYPE:-opencode}"
export EFP_SKILLS_DIR="${EFP_SKILLS_DIR:-/app/skills}"
export EFP_TOOLS_DIR="${EFP_TOOLS_DIR:-/app/tools}"
export EFP_WORKSPACE_DIR="${EFP_WORKSPACE_DIR:-/workspace}"
export EFP_ADAPTER_STATE_DIR="${EFP_ADAPTER_STATE_DIR:-/root/.local/share/efp-compat}"
export OPENCODE_DATA_DIR="${OPENCODE_DATA_DIR:-/root/.local/share/opencode}"
export EFP_OPENCODE_URL="${EFP_OPENCODE_URL:-http://127.0.0.1:4096}"
export PYTHONPATH="${EFP_TOOLS_DIR}/python:${PYTHONPATH:-}"

echo "Initializing EFP OpenCode runtime assets..."
python -m efp_opencode_adapter.init_assets

echo "opencode version $(opencode --version)"
echo "Starting opencode serve on 127.0.0.1:4096..."
opencode serve --hostname 127.0.0.1 --port 4096 &
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

python -m efp_opencode_adapter.health --wait --timeout "${EFP_OPENCODE_READY_TIMEOUT_SECONDS:-60}"

echo "Starting efp-opencode-adapter on 0.0.0.0:8000..."
python -m efp_opencode_adapter.server --host 0.0.0.0 --port 8000 --opencode-url "${EFP_OPENCODE_URL}" &
ADAPTER_PID=$!
echo "adapter listening on 0.0.0.0:8000"

wait "${ADAPTER_PID}"
