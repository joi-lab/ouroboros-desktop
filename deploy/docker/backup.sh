#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

KEEP="${KEEP:-7}"
STOP_SERVICE="${STOP_SERVICE:-1}"
SERVICE_STOPPED=0
LOCK_FILE="/var/lock/ouroboros-headless-backup.lock"

cleanup() {
  local code="$?"
  if [[ "${STOP_SERVICE}" == "1" && "${SERVICE_STOPPED}" == "1" ]]; then
    info "Ensuring service is running."
    systemctl start "${SYSTEMD_UNIT}" || true
  fi
  exit "${code}"
}
trap cleanup EXIT

require_root
validate_compose_runtime
ensure_dirs
ensure_deploy_meta_dir

[[ "${KEEP}" =~ ^[0-9]+$ ]] || die "KEEP must be a non-negative integer."
[[ "${STOP_SERVICE}" == "0" || "${STOP_SERVICE}" == "1" ]] || die "STOP_SERVICE must be 0 or 1."

exec 9>"${LOCK_FILE}"
flock -n 9 || die "Another backup process is already running."

umask 077

timestamp="$(date -u +"%Y%m%dT%H%M%SZ")"
archive="${BACKUP_DIR}/ouroboros-headless-${timestamp}.tar.gz"
checksum="${archive}.sha256"

if [[ "${STOP_SERVICE}" == "1" ]]; then
  info "Stopping service for consistent backup."
  systemctl stop "${SYSTEMD_UNIT}"
  SERVICE_STOPPED=1
fi

info "Creating backup archive ${archive}"
tar -C / -czf "${archive}" \
  "etc/ouroboros-headless/.env" \
  "var/lib/ouroboros-headless" \
  "opt/ouroboros-headless/.deploy"
chmod 600 "${archive}"

sha256sum "${archive}" > "${checksum}"
chmod 600 "${checksum}"

if [[ "${STOP_SERVICE}" == "1" ]]; then
  info "Starting service after backup."
  systemctl start "${SYSTEMD_UNIT}"
  SERVICE_STOPPED=0
fi

mapfile -t old_archives < <(ls -1t "${BACKUP_DIR}"/ouroboros-headless-*.tar.gz 2>/dev/null || true)
if [[ "${#old_archives[@]}" -gt "${KEEP}" ]]; then
  for file in "${old_archives[@]:KEEP}"; do
    rm -f "${file}" "${file}.sha256"
  done
fi

cat <<EOF
[DONE] Backup complete.
Archive: ${archive}
Checksum: ${checksum}
EOF
