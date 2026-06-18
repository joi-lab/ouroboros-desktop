#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

require_root
validate_compose_runtime
[[ -f "${ENV_FILE}" ]] || die "Missing env file ${ENV_FILE}"

previous_value="$(read_env_value "VPS_ENABLE_BROWSER_TOOLS")"
[[ -n "${previous_value}" ]] || previous_value="0"

set_env_value "VPS_ENABLE_BROWSER_TOOLS" "1"
info "VPS_ENABLE_BROWSER_TOOLS=1 saved to ${ENV_FILE}"

if ! compose up -d --build; then
  warn "Failed to enable browser tooling build. Reverting VPS_ENABLE_BROWSER_TOOLS=${previous_value}."
  set_env_value "VPS_ENABLE_BROWSER_TOOLS" "${previous_value}"
  die "Browser tooling enable failed."
fi

port="$(effective_server_port)"
if ! wait_for_health "${port}" 40 2; then
  warn "Health check failed after enabling browser tools. Reverting VPS_ENABLE_BROWSER_TOOLS=${previous_value}."
  set_env_value "VPS_ENABLE_BROWSER_TOOLS" "${previous_value}"
  if ! compose up -d --build; then
    die "Browser tooling enable failed and revert rebuild also failed. Manual intervention required: check 'docker compose logs'."
  fi
  revert_port="$(effective_server_port)"
  if ! wait_for_health "${revert_port}" 30 2; then
    die "Browser tooling enable failed and service is unhealthy after revert. Manual intervention required."
  fi
  die "Browser tooling enable failed; env setting reverted to ${previous_value}. Service is healthy on previous profile."
fi

echo "[DONE] Browser tooling enabled and runtime healthy."
