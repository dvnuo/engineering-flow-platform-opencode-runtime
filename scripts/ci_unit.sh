#!/usr/bin/env bash
set -euo pipefail

PYTEST_TIMEOUT_SECONDS="${PYTEST_TIMEOUT_SECONDS:-240}"
FULL_PYTEST_TIMEOUT_SECONDS="${FULL_PYTEST_TIMEOUT_SECONDS:-360}"

run_pytest() {
  local timeout_seconds="$1"
  shift
  echo "+ timeout ${timeout_seconds} python -m pytest $*"
  set +e
  timeout "${timeout_seconds}" python -m pytest "$@"
  local status="$?"
  set -e
  if [[ "${status}" -ne 0 ]]; then
    echo "pytest gate failed or timed out with status ${status}: python -m pytest $*" >&2
  fi
  return "${status}"
}

run_pytest_gate() {
  local label="$1"
  local timeout_seconds="$2"
  shift 2
  echo "== ${label} =="
  run_pytest "${timeout_seconds}" "$@"
}

echo "== opencode_client leak gate =="
run_pytest_gate "opencode_client leak gate" "${PYTEST_TIMEOUT_SECONDS}" -q tests/test_opencode_client.py 2>&1 | tee /tmp/opencode-client.log
! grep -q "Unclosed client session" /tmp/opencode-client.log
! grep -q "Unclosed connector" /tmp/opencode-client.log
! grep -q "PytestUnraisableExceptionWarning" /tmp/opencode-client.log

echo "== AppKey static/runtime gate =="
run_pytest_gate "AppKey static/runtime gate" "${PYTEST_TIMEOUT_SECONDS}" -q tests/test_app_keys.py
! grep -R 'from \.app_keys import \*' -n efp_opencode_adapter
! grep -R 'app.get("' -n efp_opencode_adapter
! grep -R "app.get('" -n efp_opencode_adapter
! grep -R 'request.app.get("' -n efp_opencode_adapter
! grep -R "request.app.get('" -n efp_opencode_adapter
! grep -R 'app\["' -n efp_opencode_adapter
! grep -R "app\['" -n efp_opencode_adapter
! grep -R 'request.app\["' -n efp_opencode_adapter
! grep -R "request.app\['" -n efp_opencode_adapter

echo "== pytest config gate =="
run_pytest_gate "pytest config gate" "${PYTEST_TIMEOUT_SECONDS}" -q tests/test_pytest_config.py tests/test_pytest_plugin_config.py
echo "== runtime contract default skip/import gate =="
run_pytest_gate "runtime contract default skip/import gate" "${PYTEST_TIMEOUT_SECONDS}" -q runtime_contract_tests
echo "== NotAppKeyWarning gate =="
run_pytest_gate "NotAppKeyWarning gate" "${PYTEST_TIMEOUT_SECONDS}" -q tests/test_app_keys.py -W error::aiohttp.web_exceptions.NotAppKeyWarning

echo "== P2 subset gates =="
echo "== P2 chat stream/recovery subset =="
run_pytest_gate "P2 chat stream/recovery subset" "${PYTEST_TIMEOUT_SECONDS}" -q \
  tests/test_chat_streaming.py \
  tests/test_chat_recovery_hardening.py

echo "== P2 skill/assets/capabilities subset =="
run_pytest_gate "P2 skill/assets/capabilities subset" "${PYTEST_TIMEOUT_SECONDS}" -q \
  tests/test_skill_sync.py \
  tests/test_capabilities_api.py \
  tests/test_init_assets.py

echo "== P2 event/profile subset =="
run_pytest_gate "P2 event/profile subset" "${PYTEST_TIMEOUT_SECONDS}" -q \
  tests/test_event_bridge.py \
  tests/test_runtime_profile_apply.py

echo "== P2 tasks/recovery/tools subset =="
run_pytest_gate "P2 tasks/recovery/tools subset" "${PYTEST_TIMEOUT_SECONDS}" -q \
  tests/test_tasks_api.py \
  tests/test_recovery.py \
  tests/test_tool_sync.py

echo "== full pytest =="
run_pytest_gate "full pytest" "${FULL_PYTEST_TIMEOUT_SECONDS}" -q
