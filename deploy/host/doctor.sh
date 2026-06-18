#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

[[ -f "${ENV_FILE}" ]] || die "Missing env file ${ENV_FILE}"

port="$(effective_server_port)"
host_bind="$(read_env_value "OUROBOROS_SERVER_HOST")"
[[ -n "${host_bind}" ]] || host_bind="127.0.0.1"

info "OS: $(. /etc/os-release && echo "${PRETTY_NAME:-unknown}")"
info "Python: $(python3 --version 2>/dev/null || echo missing)"
info "systemd user service: ${SERVICE_NAME}"
systemctl --user is-enabled "${SERVICE_NAME}" >/dev/null 2>&1 && info "systemd enabled: yes" || warn "systemd enabled: no"
systemctl --user is-active "${SERVICE_NAME}" >/dev/null 2>&1 && info "systemd active: yes" || warn "systemd active: no"

info "Env checks:"
telegram_token="$(read_env_value "TELEGRAM_BOT_TOKEN")"
telegram_chat_id="$(read_env_value "TELEGRAM_CHAT_ID")"
fb_root="$(read_env_value "OUROBOROS_FILE_BROWSER_DEFAULT")"
network_password="$(read_env_value "OUROBOROS_NETWORK_PASSWORD")"
browser_tools_enabled="$(read_env_value "OUROBOROS_BROWSER_TOOLS_ENABLED")"

[[ -n "${telegram_token}" ]] && info "  TELEGRAM_BOT_TOKEN: $(mask "${telegram_token}")" || info "  TELEGRAM_BOT_TOKEN: not set in .env"
[[ -n "${telegram_chat_id}" ]] && info "  TELEGRAM_CHAT_ID: $(mask "${telegram_chat_id}")" || info "  TELEGRAM_CHAT_ID: not set in .env"
[[ -n "${fb_root}" ]] && info "  OUROBOROS_FILE_BROWSER_DEFAULT: ${fb_root}" || warn "  OUROBOROS_FILE_BROWSER_DEFAULT: empty"
if [[ "${host_bind}" != "127.0.0.1" && "${host_bind}" != "localhost" && "${host_bind}" != "::1" ]]; then
  [[ -n "${network_password}" ]] && info "  OUROBOROS_NETWORK_PASSWORD: configured" || warn "  OUROBOROS_NETWORK_PASSWORD: missing for non-loopback bind"
fi
if [[ "${browser_tools_enabled}" == "1" ]]; then
  info "  Browser tools profile: enabled"
else
  info "  Browser tools profile: lean/default"
fi

if curl -fsS "http://127.0.0.1:${port}/api/health" >/dev/null; then info "/api/health: OK"; else warn "/api/health: FAIL"; fi

state_json="$(curl -fsS "http://127.0.0.1:${port}/api/state" || true)"
if [[ -n "${state_json}" ]]; then
  echo "${state_json}" | grep -q '"supervisor_ready"[[:space:]]*:[[:space:]]*true' && info "/api/state supervisor_ready: true" || warn "/api/state supervisor_ready: false"
  workers_alive="$(echo "${state_json}" | sed -n 's/.*"workers_alive"[[:space:]]*:[[:space:]]*\([0-9]\+\).*/\1/p' | head -n1)"
  if [[ -n "${workers_alive}" && "${workers_alive}" -gt 0 ]]; then info "/api/state workers_alive: ${workers_alive}"; else warn "/api/state workers_alive: ${workers_alive:-unknown}"; fi
else
  warn "/api/state: FAIL"
fi

ws_probe="$(curl -sS -i -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: SGVhZGxlc3NQUk9CRQ==' "http://127.0.0.1:${port}/ws" || true)"
if echo "${ws_probe}" | grep -q "101 Switching Protocols"; then info "/ws handshake: OK"; else warn "/ws handshake: FAIL"; fi

cat <<EOF
Open:
  http://127.0.0.1:${port}
SSH tunnel (remote host):
  ssh -L ${port}:127.0.0.1:${port} <user>@<host>
EOF