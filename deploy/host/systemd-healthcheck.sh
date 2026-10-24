#!/usr/bin/env bash
set -euo pipefail

env_file="${1:-$HOME/.env}"
state_port_file="${2:-$HOME/data/state/server_port}"

port=""
if [[ -f "${state_port_file}" ]]; then
  port="$(tr -d '[:space:]' < "${state_port_file}" || true)"
fi

if [[ ! "${port}" =~ ^[0-9]+$ ]] && [[ -f "${env_file}" ]]; then
  port="$(awk -F= '!/^#/ && /^OUROBOROS_SERVER_PORT=/ {print $2}' "${env_file}" | tail -n1 | tr -d '[:space:]')"
fi

if [[ ! "${port}" =~ ^[0-9]+$ ]]; then
  port="8765"
fi

for _ in {1..20}; do
  if curl -fsS "http://127.0.0.1:${port}/api/health" >/dev/null; then
    exit 0
  fi
  sleep 2
done

exit 1
