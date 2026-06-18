#!/usr/bin/env bash
set -euo pipefail

env_file="${1:-/etc/ouroboros-headless/.env}"
state_port_file="${2:-/var/lib/ouroboros-headless/data/state/server_port}"

port=""
if [[ -f "${state_port_file}" ]]; then
  port="$(tr -d '[:space:]' < "${state_port_file}" || true)"
fi

if [[ ! "${port}" =~ ^[0-9]+$ ]]; then
  if [[ -f "${env_file}" ]]; then
    port="$(awk -F= '!/^#/ && /^OUROBOROS_SERVER_PORT=/ {print $2}' "${env_file}" | tail -n1 | tr -d '[:space:]')"
  fi
fi

if [[ ! "${port}" =~ ^[0-9]+$ ]]; then
  port="8765"
fi

for _ in {1..20}; do
  if curl -fsS "http://127.0.0.1:${port}/api/health" >/dev/null; then
    exit 0
  fi
  if docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' ouroboros-headless 2>/dev/null | grep -q '^healthy$'; then
    exit 0
  fi
  sleep 2
done

exit 1
