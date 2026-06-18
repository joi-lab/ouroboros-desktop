#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

KEEP="${KEEP:-7}"
STOP_SERVICE="${STOP_SERVICE:-1}"
SERVICE_STOPPED=0

cleanup() {
  local code="$?"
  if [[ "${STOP_SERVICE}" == "1" && "${SERVICE_STOPPED}" == "1" ]]; then
    systemctl --user start "${SERVICE_NAME}" || true
  fi
  exit "${code}"
}
trap cleanup EXIT

[[ "${KEEP}" =~ ^[0-9]+$ ]] || die "KEEP must be integer >= 0"
[[ "${STOP_SERVICE}" == "0" || "${STOP_SERVICE}" == "1" ]] || die "STOP_SERVICE must be 0 or 1"

ensure_dirs
acquire_lock "${BACKUP_LOCK_FILE}"

timestamp="$(date -u +"%Y%m%dT%H%M%SZ")"
archive="${BACKUP_DIR}/ouroboros-desktop-${timestamp}.tar.gz"
checksum="${archive}.sha256"

if [[ "${STOP_SERVICE}" == "1" ]]; then
  info "Stopping service for consistent backup."
  systemctl --user stop "${SERVICE_NAME}"
  SERVICE_STOPPED=1
fi

umask 077
tar -C "${APP_ROOT}" -czf "${archive}" \
  "data" \
  ".env" \
  ".deploy"
chmod 600 "${archive}"
sha256sum "${archive}" > "${checksum}"
chmod 600 "${checksum}"

if [[ "${STOP_SERVICE}" == "1" ]]; then
  systemctl --user start "${SERVICE_NAME}"
  SERVICE_STOPPED=0
fi

mapfile -t old_archives < <(ls -1t "${BACKUP_DIR}"/ouroboros-desktop-*.tar.gz 2>/dev/null || true)
if [[ "${#old_archives[@]}" -gt "${KEEP}" ]]; then
  for file in "${old_archives[@]:KEEP}"; do
    rm -f "${file}" "${file}.sha256"
  done
fi

echo "[DONE] Backup complete.
Archive: ${archive}
Checksum: ${checksum}"