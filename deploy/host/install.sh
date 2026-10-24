#!/usr/bin/env bash
set -euo pipefail

REF=""
REPO_URL="https://github.com/joi-lab/ouroboros-desktop.git"
ASSET_BASE=""
ALLOW_NON_UBUNTU=0
NON_INTERACTIVE=0
ENABLE_LINGER=1
APP_USER="ouroboros"

SCRIPT_DIR=""
SCRIPT_PATH="${BASH_SOURCE[0]:-}"
if [[ -n "${SCRIPT_PATH}" && -f "${SCRIPT_PATH}" ]]; then
  SCRIPT_DIR="$(cd -- "$(dirname -- "${SCRIPT_PATH}")" >/dev/null 2>&1 && pwd)"
fi

derive_asset_base() {
  local repo_url="$1" ref="$2" cleaned owner_repo
  cleaned="${repo_url%.git}"
  case "${cleaned}" in
    https://github.com/*) owner_repo="${cleaned#https://github.com/}" ;;
    git@github.com:*) owner_repo="${cleaned#git@github.com:}" ;;
    *) return 1 ;;
  esac
  printf 'https://raw.githubusercontent.com/%s/%s/deploy/host' "${owner_repo}" "${ref}"
}

if [[ -z "${SCRIPT_DIR}" || ! -f "${SCRIPT_DIR}/common.sh" ]]; then
  bootstrap_ref="main"
  bootstrap_repo_url="${REPO_URL}"
  bootstrap_asset_base=""

  args=("$@")
  i=0
  while (( i < ${#args[@]} )); do
    case "${args[i]}" in
      --ref) (( i + 1 < ${#args[@]} )) && bootstrap_ref="${args[i+1]}"; ((i+=2)) ;;
      --repo-url) (( i + 1 < ${#args[@]} )) && bootstrap_repo_url="${args[i+1]}"; ((i+=2)) ;;
      --asset-base) (( i + 1 < ${#args[@]} )) && bootstrap_asset_base="${args[i+1]}"; ((i+=2)) ;;
      *) ((i+=1)) ;;
    esac
  done

  command -v curl >/dev/null 2>&1 || { echo "install.sh requires curl for remote bootstrap" >&2; exit 1; }
  if [[ -z "${bootstrap_asset_base}" ]]; then
    bootstrap_asset_base="$(derive_asset_base "${bootstrap_repo_url}" "${bootstrap_ref}" || true)"
  fi
  [[ -n "${bootstrap_asset_base}" ]] || bootstrap_asset_base="https://raw.githubusercontent.com/joi-lab/ouroboros-desktop/${bootstrap_ref}/deploy/host"

  tmp_common_dir="$(mktemp -d)"
  trap 'rm -rf "${tmp_common_dir}"' EXIT
  curl -fsSL "${bootstrap_asset_base}/common.sh" -o "${tmp_common_dir}/common.sh"
  SCRIPT_DIR="${tmp_common_dir}"
fi

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Options:
  --ref <tag|commit|branch>  Checkout explicit ref after clone/fetch (default: main).
  --repo-url <url>           Override repository URL.
  --asset-base <raw-url>     Optional raw asset base for remote common.sh bootstrap.
  --allow-non-ubuntu         Continue on unsupported Linux distro.
  --non-interactive          Skip prompts and keep defaults/current .env values.
  --app-user <name>          Runtime Linux user (default: ouroboros).
  --enable-linger            Enable loginctl linger for runtime user (default: on).
  --disable-linger           Disable automatic linger setup.
  -h, --help                 Show this help.
EOF
}

prompt_with_default() {
  local label="$1" default_value="$2" value=""
  read -r -p "${label} [${default_value}]: " value < /dev/tty || true
  [[ -n "${value}" ]] || value="${default_value}"
  printf '%s' "${value}"
}

prompt_yes_no() {
  local label="$1" default_yes_no="$2" hint="[y/N]" value=""
  [[ "${default_yes_no}" == "1" ]] && hint="[Y/n]"
  while true; do
    read -r -p "${label} ${hint}: " value < /dev/tty || true
    value="$(printf '%s' "${value}" | tr '[:upper:]' '[:lower:]')"
    if [[ -z "${value}" ]]; then
      printf '%s' "${default_yes_no}"
      return
    fi
    case "${value}" in
      y|yes) printf '1'; return ;;
      n|no) printf '0'; return ;;
      *) warn "Please answer y or n." ;;
    esac
  done
}

ensure_python_venv_support() {
  if python3 - <<'PY' >/dev/null 2>&1
import ensurepip
PY
  then
    return
  fi

  warn "python3-venv/ensurepip is missing; trying to install automatically."

  py_minor="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
  packages=(python3-venv)
  [[ -n "${py_minor}" ]] && packages+=("python${py_minor}-venv")

  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y && apt-get install -y "${packages[@]}" || true
  fi

  if ! python3 - <<'PY' >/dev/null 2>&1
import ensurepip
PY
  then
    die "Python venv support is missing. Install manually: apt-get install -y python3-venv python3.$(python3 -c 'import sys; print(sys.version_info.minor)')-venv"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref) REF="${2:-}"; shift 2 ;;
    --repo-url) REPO_URL="${2:-}"; shift 2 ;;
    --asset-base) ASSET_BASE="${2:-}"; shift 2 ;;
    --allow-non-ubuntu) ALLOW_NON_UBUNTU=1; shift ;;
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    --app-user) APP_USER="${2:-}"; shift 2 ;;
    --enable-linger) ENABLE_LINGER=1; shift ;;
    --disable-linger) ENABLE_LINGER=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ -n "${REF}" ]] || REF="main"
[[ -n "${APP_USER}" ]] || die "--app-user must be non-empty"

validate_ubuntu_or_override "${ALLOW_NON_UBUNTU}"
require_cmd bash
require_cmd git
require_cmd python3
require_cmd curl
require_cmd systemctl
require_cmd flock
require_cmd getent
require_cmd runuser

if [[ "$(id -u)" -ne 0 ]]; then
  die "Run install as root (or via sudo)."
fi

apt_updated=0
apt_install() {
  if [[ "${apt_updated}" == "0" ]]; then
    apt-get update -y
    apt_updated=1
  fi
  apt-get install -y "$@"
}

install_host_prereqs() {
  local missing=()
  command -v git >/dev/null 2>&1 || missing+=(git)
  command -v curl >/dev/null 2>&1 || missing+=(curl)
  command -v wget >/dev/null 2>&1 || missing+=(wget)
  command -v jq >/dev/null 2>&1 || missing+=(jq)
  command -v rg >/dev/null 2>&1 || missing+=(ripgrep)
  command -v gh >/dev/null 2>&1 || missing+=(gh)

  if [[ "${#missing[@]}" -gt 0 ]]; then
    info "Installing host prerequisites: ${missing[*]}"
    apt_install "${missing[@]}"
  fi
}

install_host_prereqs

if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  info "Creating runtime user: ${APP_USER}"
  useradd --create-home --shell /bin/bash "${APP_USER}"
fi

APP_HOME="$(getent passwd "${APP_USER}" | cut -d: -f6)"
[[ -n "${APP_HOME}" ]] || die "Failed to resolve home for ${APP_USER}"

APP_ROOT="${APP_HOME}"
REPO_DIR="${APP_ROOT}/repo"
ENV_FILE="${APP_ROOT}/.env"
DATA_DIR="${APP_ROOT}/data"
WORKSPACE_DIR="${APP_ROOT}/workspace"
BACKUP_DIR="${APP_ROOT}/backups"
DEPLOY_META_DIR="${APP_ROOT}/.deploy"
VENV_DIR="${APP_ROOT}/.venv"
SYSTEMD_UNIT_DIR="${APP_HOME}/.config/systemd/user"
SYSTEMD_UNIT_PATH="${SYSTEMD_UNIT_DIR}/${SERVICE_NAME}"
LOCK_DIR="${DATA_DIR}/state/locks"
INSTALL_LOCK_FILE="${LOCK_DIR}/install.lock"
UPDATE_LOCK_FILE="${LOCK_DIR}/update.lock"
BACKUP_LOCK_FILE="${LOCK_DIR}/backup.lock"
RESTORE_LOCK_FILE="${LOCK_DIR}/restore.lock"

run_as_app_user() {
  runuser -u "${APP_USER}" -- "$@"
}

systemctl_user() {
  if systemctl --machine="${APP_USER}@.host" --user "$@"; then
    return 0
  fi

  local app_uid
  app_uid="$(id -u "${APP_USER}")"
  runuser -u "${APP_USER}" -- env \
    XDG_RUNTIME_DIR="/run/user/${app_uid}" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${app_uid}/bus" \
    systemctl --user "$@"
}

ensure_dirs
chown -R "${APP_USER}:${APP_USER}" "${APP_ROOT}"
acquire_lock "${INSTALL_LOCK_FILE}"

if [[ -d "${REPO_DIR}/.git" ]]; then
  info "Repository exists at ${REPO_DIR}; refreshing deploy remote."
elif [[ -d "${REPO_DIR}" ]]; then
  if [[ -z "$(ls -A "${REPO_DIR}" 2>/dev/null || true)" ]]; then
    run_as_app_user git clone "${REPO_URL}" "${REPO_DIR}"
  else
    backup_repo_dir="${REPO_DIR}.preinstall.$(date -u +%Y%m%d-%H%M%S)"
    warn "${REPO_DIR} exists and is not a git checkout. Backing up -> ${backup_repo_dir}"
    mv "${REPO_DIR}" "${backup_repo_dir}"
    chown -R "${APP_USER}:${APP_USER}" "${backup_repo_dir}" || true
    run_as_app_user git clone "${REPO_URL}" "${REPO_DIR}"
  fi
else
  run_as_app_user git clone "${REPO_URL}" "${REPO_DIR}"
fi

# install runs as root, but repo owner is APP_USER; mark safe for root-side git helpers
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0="safe.directory"
export GIT_CONFIG_VALUE_0="${REPO_DIR}"
git config --global --add safe.directory "${REPO_DIR}" || true

ensure_seed_remote "${REPO_URL}"
run_as_app_user git -C "${REPO_DIR}" fetch "$(deploy_remote_name)" --tags
resolved_ref="$(resolve_checkout_ref "${REF}")"
run_as_app_user git -C "${REPO_DIR}" checkout "${resolved_ref}"

if [[ ! -f "${ENV_FILE}" ]]; then
  cp "${REPO_DIR}/deploy/host/.env.example" "${ENV_FILE}"
fi
chmod 600 "${ENV_FILE}"

set_env_value "OUROBOROS_APP_ROOT" "${APP_ROOT}"
set_env_value "OUROBOROS_REPO_DIR" "${REPO_DIR}"
set_env_value "OUROBOROS_DATA_DIR" "${DATA_DIR}"

host_bind="$(read_env_value "OUROBOROS_SERVER_HOST")"
[[ -n "${host_bind}" ]] || host_bind="127.0.0.1"
server_port_value="$(read_env_value "OUROBOROS_SERVER_PORT")"
[[ "${server_port_value}" =~ ^[0-9]+$ ]] || server_port_value="8765"
file_browser_root="$(read_env_value "OUROBOROS_FILE_BROWSER_DEFAULT")"
[[ -n "${file_browser_root}" ]] || file_browser_root="${WORKSPACE_DIR}"
network_password="$(read_env_value "OUROBOROS_NETWORK_PASSWORD")"
browser_tools_enabled="$(read_env_value "OUROBOROS_BROWSER_TOOLS_ENABLED")"
[[ "${browser_tools_enabled}" == "1" ]] || browser_tools_enabled="0"

interactive=1
if [[ "${NON_INTERACTIVE}" == "1" || ! -r /dev/tty ]]; then
  interactive=0
fi

if [[ "${interactive}" == "1" ]]; then
  host_bind="$(prompt_with_default "OUROBOROS_SERVER_HOST" "${host_bind}")"
  server_port_value="$(prompt_with_default "OUROBOROS_SERVER_PORT" "${server_port_value}")"
  file_browser_root="$(prompt_with_default "OUROBOROS_FILE_BROWSER_DEFAULT" "${file_browser_root}")"
  browser_tools_enabled="$(prompt_yes_no "Enable browser tools (Playwright/Chromium deps)?" "${browser_tools_enabled}")"
fi

mkdir -p "${file_browser_root}" "${WORKSPACE_DIR}" "${DATA_DIR}" "${BACKUP_DIR}" "${DEPLOY_META_DIR}"
chown -R "${APP_USER}:${APP_USER}" "${APP_ROOT}"

# Legacy path compatibility shim for upstream defaults that still reference
# ~/Ouroboros/... . Map it to the real home root used by this deploy profile.
legacy_root="${APP_HOME}/Ouroboros"
if [[ -L "${legacy_root}" ]]; then
  legacy_target="$(readlink "${legacy_root}" || true)"
  if [[ "${legacy_target}" != "${APP_HOME}" ]]; then
    rm -f "${legacy_root}"
    ln -s "${APP_HOME}" "${legacy_root}"
  fi
elif [[ -e "${legacy_root}" ]]; then
  if [[ -z "$(ls -A "${legacy_root}" 2>/dev/null || true)" ]]; then
    rmdir "${legacy_root}"
    ln -s "${APP_HOME}" "${legacy_root}"
  else
    compat_backup="${legacy_root}.prelink.$(date -u +%Y%m%d-%H%M%S)"
    warn "${legacy_root} exists and is not empty. Moving to ${compat_backup} and creating compatibility symlink."
    mv "${legacy_root}" "${compat_backup}"
    chown -R "${APP_USER}:${APP_USER}" "${compat_backup}" || true
    ln -s "${APP_HOME}" "${legacy_root}"
  fi
else
  ln -s "${APP_HOME}" "${legacy_root}"
fi

set_env_value "OUROBOROS_SERVER_HOST" "${host_bind}"
set_env_value "OUROBOROS_SERVER_PORT" "${server_port_value}"
set_env_value "OUROBOROS_FILE_BROWSER_DEFAULT" "${file_browser_root}"

if [[ "${host_bind}" != "127.0.0.1" && "${host_bind}" != "localhost" && "${host_bind}" != "::1" && -z "${network_password}" ]]; then
  die "OUROBOROS_NETWORK_PASSWORD is required for non-loopback bind."
fi

set_env_value "OUROBOROS_BROWSER_TOOLS_ENABLED" "${browser_tools_enabled}"
validate_security_contracts

ensure_python_venv_support
run_as_app_user python3 -m venv "${VENV_DIR}"
run_as_app_user "$(venv_python)" -m pip install --upgrade pip setuptools wheel
run_as_app_user "$(venv_python)" -m pip install -r "${REPO_DIR}/requirements.txt"
run_as_app_user "$(venv_python)" -m pip install -U pytest

run_as_app_user mkdir -p "${SYSTEMD_UNIT_DIR}"
sed \
  -e "s|__APP_ROOT__|${APP_ROOT}|g" \
  -e "s|__REPO_DIR__|${REPO_DIR}|g" \
  -e "s|__ENV_FILE__|${ENV_FILE}|g" \
  -e "s|__VENV_PYTHON__|$(venv_python)|g" \
  "${REPO_DIR}/deploy/host/ouroboros.service" > "${SYSTEMD_UNIT_PATH}"
chown "${APP_USER}:${APP_USER}" "${SYSTEMD_UNIT_PATH}"

if [[ "${ENABLE_LINGER}" == "1" ]] && command -v loginctl >/dev/null 2>&1; then
  loginctl enable-linger "${APP_USER}" || warn "Failed to enable linger automatically."
fi
if command -v loginctl >/dev/null 2>&1; then
  loginctl start-user "${APP_USER}" >/dev/null 2>&1 || true
fi

if [[ "${browser_tools_enabled}" == "1" ]]; then
  info "Installing browser tools host dependencies..."
  HOME="${APP_HOME}" "${REPO_DIR}/deploy/host/enable-browser-tools.sh"
fi

# Pre-clean stray runtime processes not managed by the current systemd user unit.
# This avoids split-brain startup where old server.py workers keep running with
# fallback paths while a fresh unit starts with updated env.
main_pid="$(systemctl_user show "${SERVICE_NAME}" -p MainPID --value 2>/dev/null || true)"
for pid in $(pgrep -u "${APP_USER}" -f "${VENV_DIR}/bin/python ${REPO_DIR}/server.py" || true); do
  if [[ -n "${main_pid}" && "${pid}" == "${main_pid}" ]]; then
    continue
  fi
  kill -TERM "${pid}" 2>/dev/null || true
done
sleep 1
for pid in $(pgrep -u "${APP_USER}" -f "${VENV_DIR}/bin/python ${REPO_DIR}/server.py" || true); do
  if [[ -n "${main_pid}" && "${pid}" == "${main_pid}" ]]; then
    continue
  fi
  kill -KILL "${pid}" 2>/dev/null || true
done

systemctl_user daemon-reload
systemctl_user enable "${SERVICE_NAME}"
systemctl_user restart "${SERVICE_NAME}"

port="$(effective_server_port)"
wait_for_health "${port}" 90 2 || die "Health check failed after install (port ${port})."

current_ref="$(git_ref)"
record_ref_metadata "${current_ref}" ""

echo "[DONE] Ouroboros Desktop home-host profile installed.
Runtime user: ${APP_USER}
Runtime home: ${APP_HOME}
Status: runuser -u ${APP_USER} -- systemctl --user status ${SERVICE_NAME}
Logs: runuser -u ${APP_USER} -- journalctl --user -u ${SERVICE_NAME} -f
Health: curl -fsS http://127.0.0.1:${port}/api/health
Manual run (diagnostics): runuser -u ${APP_USER} -- bash -lc 'set -a; source ${ENV_FILE}; set +a; cd ${REPO_DIR} && $(venv_python) server.py'
Update: runuser -u ${APP_USER} -- ${REPO_DIR}/deploy/host/update.sh --ref <tag-or-commit>
Backup: runuser -u ${APP_USER} -- ${REPO_DIR}/deploy/host/backup.sh
Restore: runuser -u ${APP_USER} -- ${REPO_DIR}/deploy/host/restore.sh <backup-archive.tar.gz>
Current ref: ${current_ref}"
