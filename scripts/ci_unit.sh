#!/usr/bin/env bash
set -euo pipefail

PYTEST_TIMEOUT_SECONDS="${PYTEST_TIMEOUT_SECONDS:-240}"
FULL_PYTEST_TIMEOUT_SECONDS="${FULL_PYTEST_TIMEOUT_SECONDS:-360}"

run_pytest() {
  local timeout_seconds="$1"
  shift
  timeout "${timeout_seconds}" python -m pytest "$@"
}

echo "== opencode_client leak gate =="
run_pytest "${PYTEST_TIMEOUT_SECONDS}" -q tests/test_opencode_client.py 2>&1 | tee /tmp/opencode-client.log
! grep -q "Unclosed client session" /tmp/opencode-client.log
! grep -q "Unclosed connector" /tmp/opencode-client.log
! grep -q "PytestUnraisableExceptionWarning" /tmp/opencode-client.log

echo "== AppKey static/runtime gate =="
run_pytest "${PYTEST_TIMEOUT_SECONDS}" -q tests/test_app_keys.py
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
run_pytest "${PYTEST_TIMEOUT_SECONDS}" -q tests/test_pytest_config.py tests/test_pytest_plugin_config.py

echo "== NotAppKeyWarning gate =="
run_pytest "${PYTEST_TIMEOUT_SECONDS}" -q tests/test_app_keys.py -W error::aiohttp.web_exceptions.NotAppKeyWarning

echo "== P2 subset =="
run_pytest "${PYTEST_TIMEOUT_SECONDS}" -q \
  tests/test_chat_streaming.py \
  tests/test_chat_recovery_hardening.py \
  tests/test_skill_sync.py \
  tests/test_capabilities_api.py \
  tests/test_init_assets.py \
  tests/test_event_bridge.py \
  tests/test_runtime_profile_apply.py \
  tests/test_tasks_api.py \
  tests/test_recovery.py \
  tests/test_tool_sync.py

echo "== full pytest =="
run_pytest "${FULL_PYTEST_TIMEOUT_SECONDS}" -q
