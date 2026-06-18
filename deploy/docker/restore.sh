#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

ARCHIVE_PATH="${1:-}"
[[ -n "${ARCHIVE_PATH}" ]] || die "Usage: restore.sh <backup-archive.tar.gz>"

require_root
validate_compose_runtime
[[ -d "${REPO_DIR}/.git" ]] || die "Repository not found at ${REPO_DIR}."
[[ -f "${ARCHIVE_PATH}" ]] || die "Archive not found: ${ARCHIVE_PATH}"
[[ -f "${ARCHIVE_PATH}.sha256" ]] || die "Checksum file missing: ${ARCHIVE_PATH}.sha256"

SERVICE_STOPPED=0
staging_root="${BACKUP_DIR}/.restore-staging"
mkdir -p "${staging_root}"
tmpdir="$(mktemp -d "${staging_root}/restore.XXXXXX")"

data_backup_dir=""
deploy_backup_dir=""

cleanup() {
  local code="$?"

  if [[ "${code}" -ne 0 ]]; then
    warn "Restore failed; attempting rollback to pre-restore state."

    if [[ -n "${data_backup_dir}" && -d "${data_backup_dir}" ]]; then
      rm -rf "/var/lib/ouroboros-headless" || true
      mv "${data_backup_dir}" "/var/lib/ouroboros-headless" || true
    fi

    if [[ -n "${deploy_backup_dir}" && -d "${deploy_backup_dir}" ]]; then
      rm -rf "/opt/ouroboros-headless/.deploy" || true
      mv "${deploy_backup_dir}" "/opt/ouroboros-headless/.deploy" || true
    fi
  else
    rm -rf "${data_backup_dir}" "${deploy_backup_dir}"
  fi

  rm -rf "${tmpdir}"

  if [[ "${SERVICE_STOPPED}" == "1" ]]; then
    info "Ensuring service is running."
    systemctl start "${SYSTEMD_UNIT}" || true
  fi

  exit "${code}"
}
trap cleanup EXIT

(
  cd "$(dirname "${ARCHIVE_PATH}")"
  sha256sum -c "$(basename "${ARCHIVE_PATH}").sha256"
)

info "Stopping service."
systemctl stop "${SYSTEMD_UNIT}"
SERVICE_STOPPED=1

info "Extracting archive."
tar -C "${tmpdir}" -xzf "${ARCHIVE_PATH}"

if [[ -d "${tmpdir}/var/lib/ouroboros-headless" ]]; then
  data_backup_dir="/var/lib/ouroboros-headless.pre-restore.$(date +%s)"
  if [[ -d "/var/lib/ouroboros-headless" ]]; then
    mv "/var/lib/ouroboros-headless" "${data_backup_dir}"
  fi
  mv "${tmpdir}/var/lib/ouroboros-headless" "/var/lib/ouroboros-headless"
fi

if [[ -f "${tmpdir}/etc/ouroboros-headless/.env" ]]; then
  mkdir -p "/etc/ouroboros-headless"
  cp "${tmpdir}/etc/ouroboros-headless/.env" "/etc/ouroboros-headless/.env"
  chmod 600 "/etc/ouroboros-headless/.env"
fi

if [[ -d "${tmpdir}/opt/ouroboros-headless/.deploy" ]]; then
  mkdir -p "/opt/ouroboros-headless"
  deploy_backup_dir="/opt/ouroboros-headless/.deploy.pre-restore.$(date +%s)"
  if [[ -d "/opt/ouroboros-headless/.deploy" ]]; then
    mv "/opt/ouroboros-headless/.deploy" "${deploy_backup_dir}"
  fi
  mv "${tmpdir}/opt/ouroboros-headless/.deploy" "/opt/ouroboros-headless/.deploy"
fi

info "Starting service."
systemctl start "${SYSTEMD_UNIT}"

port="$(effective_server_port)"
wait_for_health "${port}" 30 2 || die "Health check failed after restore (port ${port})."

SERVICE_STOPPED=0
echo "[DONE] Restore finished and health check passed."
