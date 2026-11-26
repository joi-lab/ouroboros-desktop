#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

ARCHIVE_PATH="${1:-}"
[[ -n "${ARCHIVE_PATH}" ]] || die "Usage: restore.sh <backup-archive.tar.gz>"
[[ -f "${ARCHIVE_PATH}" ]] || die "Archive not found: ${ARCHIVE_PATH}"
[[ -f "${ARCHIVE_PATH}.sha256" ]] || die "Checksum file missing: ${ARCHIVE_PATH}.sha256"

acquire_lock "${RESTORE_LOCK_FILE}"

SERVICE_STOPPED=0
tmpdir="$(mktemp -d "${BACKUP_DIR}/restore.XXXXXX")"
data_backup_dir=""
deploy_backup_dir=""

after_fail_restore() {
  warn "Restore failed; attempting rollback to pre-restore state."
  if [[ -n "${data_backup_dir}" && -d "${data_backup_dir}" ]]; then
    rm -rf "${DATA_DIR}" || true
    mv "${data_backup_dir}" "${DATA_DIR}" || true
  fi
  if [[ -n "${deploy_backup_dir}" && -d "${deploy_backup_dir}" ]]; then
    rm -rf "${DEPLOY_META_DIR}" || true
    mv "${deploy_backup_dir}" "${DEPLOY_META_DIR}" || true
  fi
}

cleanup() {
  local code="$?"
  if [[ "${code}" -ne 0 ]]; then
    after_fail_restore
  else
    rm -rf "${data_backup_dir}" "${deploy_backup_dir}"
  fi
  rm -rf "${tmpdir}"
  if [[ "${SERVICE_STOPPED}" == "1" ]]; then
    systemctl --user start "${SERVICE_NAME}" || true
  fi
  exit "${code}"
}
trap cleanup EXIT

(
  cd "$(dirname "${ARCHIVE_PATH}")"
  sha256sum -c "$(basename "${ARCHIVE_PATH}").sha256"
)

systemctl --user stop "${SERVICE_NAME}"
SERVICE_STOPPED=1

tar -C "${tmpdir}" -xzf "${ARCHIVE_PATH}"

if [[ -d "${tmpdir}/data" ]]; then
  data_backup_dir="${APP_ROOT}/data.pre-restore.$(date +%s)"
  [[ -d "${DATA_DIR}" ]] && mv "${DATA_DIR}" "${data_backup_dir}"
  mv "${tmpdir}/data" "${DATA_DIR}"
fi

if [[ -f "${tmpdir}/.env" ]]; then
  cp "${tmpdir}/.env" "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
fi

if [[ -d "${tmpdir}/.deploy" ]]; then
  deploy_backup_dir="${APP_ROOT}/.deploy.pre-restore.$(date +%s)"
  [[ -d "${DEPLOY_META_DIR}" ]] && mv "${DEPLOY_META_DIR}" "${deploy_backup_dir}"
  mv "${tmpdir}/.deploy" "${DEPLOY_META_DIR}"
fi

systemctl --user start "${SERVICE_NAME}"
port="$(effective_server_port)"
wait_for_health "${port}" 40 2 || die "Health check failed after restore (port ${port})."

SERVICE_STOPPED=0
echo "[DONE] Restore finished and health check passed."