#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

require_root
validate_compose_runtime
[[ -f "${ENV_FILE}" ]] || die "Missing env file ${ENV_FILE}"
port="$(effective_server_port)"
host_bind="$(read_env_value "OUROBOROS_HOST_BIND")"
if [[ -z "${host_bind}" ]]; then
  host_bind="$(read_env_value "OUROBOROS_SERVER_HOST")"
fi
[[ -n "${host_bind}" ]] || host_bind="127.0.0.1"

info "OS: $(. /etc/os-release && echo "${PRETTY_NAME:-unknown}")"
info "Docker: $(docker --version)"
info "Compose: $(docker compose version --short)"
if command -v gh >/dev/null 2>&1; then
  info "Host gh: $(gh --version | head -n1)"
else
  warn "Host gh: missing"
fi
info "Service: ${SYSTEMD_UNIT}"
systemctl is-enabled "${SYSTEMD_UNIT}" >/dev/null 2>&1 && info "systemd enabled: yes" || warn "systemd enabled: no"
systemctl is-active "${SYSTEMD_UNIT}" >/dev/null 2>&1 && info "systemd active: yes" || warn "systemd active: no"

info "Env checks:"
telegram_token="$(read_env_value "TELEGRAM_BOT_TOKEN")"
telegram_chat_id="$(read_env_value "TELEGRAM_CHAT_ID")"
fb_root="$(read_env_value "OUROBOROS_FILE_BROWSER_DEFAULT")"
network_password="$(read_env_value "OUROBOROS_NETWORK_PASSWORD")"

[[ -n "${telegram_token}" ]] && info "  TELEGRAM_BOT_TOKEN: $(mask "${telegram_token}")" || info "  TELEGRAM_BOT_TOKEN: not set in .env (can be configured in Dashboard)"
[[ -n "${telegram_chat_id}" ]] && info "  TELEGRAM_CHAT_ID: $(mask "${telegram_chat_id}")" || info "  TELEGRAM_CHAT_ID: not set in .env (dashboard/runtime may set active chat)"
[[ -n "${fb_root}" ]] && info "  OUROBOROS_FILE_BROWSER_DEFAULT: ${fb_root}" || warn "  OUROBOROS_FILE_BROWSER_DEFAULT: empty"
if [[ "${host_bind}" != "127.0.0.1" && "${host_bind}" != "localhost" && "${host_bind}" != "::1" ]]; then
  [[ -n "${network_password}" ]] && info "  OUROBOROS_NETWORK_PASSWORD: configured" || warn "  OUROBOROS_NETWORK_PASSWORD: missing for non-loopback bind"
fi

if curl -fsS "http://127.0.0.1:${port}/api/health" >/dev/null; then
  info "/api/health: OK"
else
  warn "/api/health: FAIL"
fi

state_json="$(curl -fsS "http://127.0.0.1:${port}/api/state" || true)"
if [[ -n "${state_json}" ]]; then
  if echo "${state_json}" | grep -q '"supervisor_ready"[[:space:]]*:[[:space:]]*true'; then
    info "/api/state supervisor_ready: true"
  else
    warn "/api/state supervisor_ready: false"
  fi
  workers_alive="$(echo "${state_json}" | sed -n 's/.*"workers_alive"[[:space:]]*:[[:space:]]*\([0-9]\+\).*/\1/p' | head -n1)"
  if [[ -n "${workers_alive}" ]]; then
    if [[ "${workers_alive}" -gt 0 ]]; then
      info "/api/state workers_alive: ${workers_alive}"
    else
      warn "/api/state workers_alive: 0"
    fi
  else
    warn "/api/state workers_alive: unknown"
  fi
else
  warn "/api/state: FAIL"
fi

ws_probe="$(curl -sS -i \
  -H "Connection: Upgrade" \
  -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: SGVhZGxlc3NQUk9CRQ==" \
  "http://127.0.0.1:${port}/ws" || true)"
if echo "${ws_probe}" | grep -q "101 Switching Protocols"; then
  info "/ws handshake: OK"
else
  warn "/ws handshake: FAIL"
fi

if compose ps >/dev/null 2>&1; then
  compose ps
else
  warn "docker compose ps failed."
fi

repo_dir_host="$(read_env_value "OUROBOROS_REPO_DIR_HOST")"
[[ -n "${repo_dir_host}" ]] || repo_dir_host="/opt/ouroboros-headless"
if docker ps --format '{{.Names}}' | grep -qx 'ouroboros-headless'; then
  if docker exec ouroboros-headless sh -lc '[ -d /app/.git ]'; then
    info "Container repo mount: /app/.git present"
  else
    warn "Container repo mount: /app/.git missing (bind-mount may be broken)"
  fi

  mount_src="$(docker inspect ouroboros-headless --format '{{range .Mounts}}{{if eq .Destination "/app"}}{{.Source}}{{end}}{{end}}' 2>/dev/null || true)"
  if [[ -n "${mount_src}" ]]; then
    if [[ "${mount_src}" == "${repo_dir_host}" ]]; then
      info "Container /app mount source: ${mount_src}"
    else
      warn "Container /app mount source mismatch: ${mount_src} (expected ${repo_dir_host})"
    fi
  else
    warn "Container /app mount source: not found"
  fi

  for bin in git gh curl wget jq rg python3 pytest node npm; do
    if docker exec ouroboros-headless sh -lc "command -v ${bin} >/dev/null 2>&1"; then
      info "Container ${bin}: present"
    else
      warn "Container ${bin}: missing"
    fi
  done
fi

if [[ -f "${DEPLOY_META_DIR}/current_ref" ]]; then
  info "Current deploy ref: $(cat "${DEPLOY_META_DIR}/current_ref")"
fi
if [[ -f "${DEPLOY_META_DIR}/previous_ref" ]]; then
  info "Previous deploy ref: $(cat "${DEPLOY_META_DIR}/previous_ref")"
fi

cat <<EOF
Tunnel:
  ssh -L ${port}:127.0.0.1:${port} <user>@<vps>
Open:
  http://127.0.0.1:${port}
EOF
