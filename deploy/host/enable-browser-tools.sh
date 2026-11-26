#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

[[ -f "${ENV_FILE}" ]] || die "Missing env file ${ENV_FILE}"
require_cmd apt-get

set_env_value "OUROBOROS_BROWSER_TOOLS_ENABLED" "1"

if command -v playwright >/dev/null 2>&1; then
  info "Playwright already installed."
else
  info "Installing browser deps (Playwright/Chromium path)."
  apt-get update -y || warn "apt-get update failed (continue manually if needed)."
  apt-get install -y libnss3 libatk-bridge2.0-0 libxkbcommon0 libgtk-3-0 libasound2t64 || true
fi

echo "[DONE] Browser tools profile enabled in ${ENV_FILE}. If your runtime needs extra deps, install them and restart: systemctl --user restart ${SERVICE_NAME}"