#!/usr/bin/env bash
set -euo pipefail

export HOME="${HOME:-/root}"
export OPENCODE_CONFIG="${OPENCODE_CONFIG:-/workspace/.opencode/opencode.json}"
export EFP_RUNTIME_TYPE="${EFP_RUNTIME_TYPE:-opencode}"
export EFP_SKILLS_DIR="${EFP_SKILLS_DIR:-/app/skills}"
export EFP_WORKSPACE_DIR="${EFP_WORKSPACE_DIR:-/workspace}"
export EFP_ADAPTER_STATE_DIR="${EFP_ADAPTER_STATE_DIR:-/root/.local/share/efp-compat}"
export OPENCODE_DATA_DIR="${OPENCODE_DATA_DIR:-/root/.local/share/opencode}"
export EFP_OPENCODE_URL="${EFP_OPENCODE_URL:-http://127.0.0.1:4096}"

echo "Initializing EFP OpenCode runtime assets..."
python -m efp_opencode_adapter.init_assets


echo "Bootstrapping OpenCode runtime profile from Portal context..."
python -m efp_opencode_adapter.portal_runtime_context_bootstrap \
  --workspace-dir "${EFP_WORKSPACE_DIR}"

echo "opencode version $(opencode --version)"
echo "Starting efp-opencode-adapter on 0.0.0.0:8000 with managed OpenCode serve..."
exec python -m efp_opencode_adapter.server \
  --host 0.0.0.0 \
  --port 8000 \
  --opencode-url "${EFP_OPENCODE_URL}" \
  --manage-opencode
