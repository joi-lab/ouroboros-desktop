#!/usr/bin/env bash
set -euo pipefail

APP_NAME="ouroboros-desktop"
APP_ROOT="${HOME}"
REPO_DIR="${APP_ROOT}/repo"
ENV_FILE="${APP_ROOT}/.env"
DATA_DIR="${APP_ROOT}/data"
WORKSPACE_DIR="${APP_ROOT}/workspace"
BACKUP_DIR="${APP_ROOT}/backups"
DEPLOY_META_DIR="${APP_ROOT}/.deploy"
VENV_DIR="${APP_ROOT}/.venv"
SERVICE_NAME="ouroboros.service"
SYSTEMD_UNIT_DIR="${HOME}/.config/systemd/user"
SYSTEMD_UNIT_PATH="${SYSTEMD_UNIT_DIR}/${SERVICE_NAME}"
SEED_REMOTE_NAME="seed"
LOCK_DIR="${DATA_DIR}/state/locks"
INSTALL_LOCK_FILE="${LOCK_DIR}/install.lock"
UPDATE_LOCK_FILE="${LOCK_DIR}/update.lock"
BACKUP_LOCK_FILE="${LOCK_DIR}/backup.lock"
RESTORE_LOCK_FILE="${LOCK_DIR}/restore.lock"

SCRIPT_NAME="$(basename "${0}")"

info() { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
error() { printf '[ERROR] %s\n' "$*" >&2; }
die() { error "$*"; exit 1; }

on_error() {
  local code="$?"
  error "${SCRIPT_NAME} failed at line ${BASH_LINENO[0]} (exit ${code})."
  exit "${code}"
}
trap on_error ERR

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing command: $1"
}

mask() {
  local value="${1:-}"
  if [[ -z "${value}" ]]; then
    printf '<empty>'
    return
  fi
  if [[ "${#value}" -le 8 ]]; then
    printf '********'
    return
  fi
  printf '%s***' "${value:0:8}"
}

ubuntu_supported() {
  [[ -f /etc/os-release ]] || return 1
  # shellcheck disable=SC1091
  source /etc/os-release
  [[ "${ID:-}" == "ubuntu" ]] || return 1
  [[ "${VERSION_ID:-}" == "22.04" || "${VERSION_ID:-}" == "24.04" ]]
}

validate_ubuntu_or_override() {
  local allow_non_ubuntu="${1:-0}"
  if ubuntu_supported; then
    return
  fi
  if [[ "${allow_non_ubuntu}" == "1" ]]; then
    warn "Non-Ubuntu host detected; unsupported for MVP, continuing due to override."
    return
  fi
  die "Unsupported OS for MVP. Use --allow-non-ubuntu to continue at your own risk."
}

ensure_dirs() {
  mkdir -p "${APP_ROOT}" "${DATA_DIR}" "${WORKSPACE_DIR}" "${BACKUP_DIR}" "${DEPLOY_META_DIR}" "${LOCK_DIR}"
}

read_env_value() {
  local key="$1"
  [[ -f "${ENV_FILE}" ]] || { printf ''; return; }

  python3 - "${ENV_FILE}" "${key}" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]
value = ""

for line in path.read_text(encoding="utf-8").splitlines():
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in line:
        continue
    k, raw = line.split("=", 1)
    if k.strip() != key:
        continue
    v = raw.strip()
    if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
        v = v[1:-1]
    value = v

sys.stdout.write(value)
PY
}

set_env_value() {
  local key="$1"
  local value="$2"
  [[ -f "${ENV_FILE}" ]] || die "Missing env file: ${ENV_FILE}"

  python3 - "${ENV_FILE}" "${key}" "${value}" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines()

updated = False
out = []
for line in lines:
    stripped = line.lstrip()
    if stripped.startswith("#") or "=" not in line:
        out.append(line)
        continue
    k = line.split("=", 1)[0].strip()
    if k == key:
      if not updated:
        out.append(f"{key}={value}")
        updated = True
      continue
    out.append(line)

if not updated:
    if out and out[-1] != "":
        out.append("")
    out.append(f"{key}={value}")

path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
}

server_port() {
  local port
  port="$(read_env_value "OUROBOROS_SERVER_PORT")"
  [[ -n "${port}" ]] || port="8765"
  printf '%s' "${port}"
}

effective_server_port() {
  local runtime_port_file port
  runtime_port_file="${DATA_DIR}/state/server_port"
  if [[ -f "${runtime_port_file}" ]]; then
    port="$(tr -d '[:space:]' < "${runtime_port_file}" || true)"
    if [[ "${port}" =~ ^[0-9]+$ ]]; then
      printf '%s' "${port}"
      return
    fi
  fi
  server_port
}

validate_security_contracts() {
  local host_bind password fb_root
  host_bind="$(read_env_value "OUROBOROS_SERVER_HOST")"
  password="$(read_env_value "OUROBOROS_NETWORK_PASSWORD")"
  fb_root="$(read_env_value "OUROBOROS_FILE_BROWSER_DEFAULT")"

  [[ -n "${host_bind}" ]] || host_bind="127.0.0.1"
  [[ -n "${fb_root}" ]] || die "OUROBOROS_FILE_BROWSER_DEFAULT must be set."
  [[ -d "${fb_root}" ]] || die "OUROBOROS_FILE_BROWSER_DEFAULT must exist and be a directory: ${fb_root}"

  if [[ "${host_bind}" != "127.0.0.1" && "${host_bind}" != "localhost" && "${host_bind}" != "::1" ]]; then
    [[ -n "${password}" ]] || die "OUROBOROS_NETWORK_PASSWORD is mandatory for non-loopback bind."
  fi
}

wait_for_health() {
  local port attempts sleep_seconds i host_bind host url_host
  port="${1:-$(server_port)}"
  attempts="${2:-20}"
  sleep_seconds="${3:-2}"

  host_bind="$(read_env_value "OUROBOROS_SERVER_HOST")"
  [[ -n "${host_bind}" ]] || host_bind="127.0.0.1"

  for ((i = 1; i <= attempts; i++)); do
    for host in "${host_bind}" "127.0.0.1"; do
      [[ "${host}" == "0.0.0.0" || "${host}" == "::" ]] && host="127.0.0.1"
      if [[ "${host}" == *:* ]]; then
        url_host="[${host}]"
      else
        url_host="${host}"
      fi

      if curl -fsS "http://${url_host}:${port}/api/health" >/dev/null 2>&1; then
        return 0
      fi
    done
    sleep "${sleep_seconds}"
  done
  return 1
}

record_ref_metadata() {
  local current_ref="${1:-}"
  local previous_ref="${2:-}"
  mkdir -p "${DEPLOY_META_DIR}"
  [[ -n "${current_ref}" ]] && printf '%s\n' "${current_ref}" > "${DEPLOY_META_DIR}/current_ref"
  [[ -n "${previous_ref}" ]] && printf '%s\n' "${previous_ref}" > "${DEPLOY_META_DIR}/previous_ref"
  printf '%s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" > "${DEPLOY_META_DIR}/last_deploy_utc"
}

git_ref() {
  git -C "${REPO_DIR}" rev-parse --short=12 HEAD
}

has_git_remote() {
  local remote_name="$1"
  git -C "${REPO_DIR}" remote | grep -Fxq "${remote_name}"
}

git_remote_url() {
  local remote_name="$1"
  git -C "${REPO_DIR}" remote get-url "${remote_name}" 2>/dev/null || true
}

deploy_remote_name() {
  if has_git_remote "${SEED_REMOTE_NAME}"; then
    printf '%s' "${SEED_REMOTE_NAME}"
    return
  fi
  if has_git_remote "origin"; then
    printf '%s' "origin"
    return
  fi
  printf ''
}

ensure_seed_remote() {
  local seed_url="$1"
  local origin_url current_seed_url

  [[ -n "${seed_url}" ]] || die "seed remote URL is required"

  origin_url="$(git_remote_url "origin")"
  current_seed_url="$(git_remote_url "${SEED_REMOTE_NAME}")"

  if [[ -z "${current_seed_url}" ]]; then
    if [[ -n "${origin_url}" && "${origin_url}" == "${seed_url}" ]]; then
      git -C "${REPO_DIR}" remote rename origin "${SEED_REMOTE_NAME}"
      origin_url=""
      current_seed_url="${seed_url}"
      info "Renamed origin -> ${SEED_REMOTE_NAME}."
    else
      git -C "${REPO_DIR}" remote add "${SEED_REMOTE_NAME}" "${seed_url}"
      current_seed_url="${seed_url}"
      info "Configured ${SEED_REMOTE_NAME} remote for upstream updates."
    fi
  fi

  if [[ "${current_seed_url}" != "${seed_url}" ]]; then
    git -C "${REPO_DIR}" remote set-url "${SEED_REMOTE_NAME}" "${seed_url}"
    info "Updated ${SEED_REMOTE_NAME} remote URL."
  fi

  if [[ -n "${origin_url}" && "${origin_url}" == "${seed_url}" ]]; then
    git -C "${REPO_DIR}" remote remove origin
    info "Detached runtime repo from installer upstream (origin removed)."
  fi
}

fetch_deploy_remote() {
  local remote
  remote="$(deploy_remote_name)"
  [[ -n "${remote}" ]] || die "No git remote configured for updates (expected ${SEED_REMOTE_NAME} or origin)."
  git -C "${REPO_DIR}" fetch "${remote}" --tags
}

resolve_checkout_ref() {
  local target="$1"
  local remote

  [[ -n "${target}" ]] || die "resolve_checkout_ref requires target ref"

  if git -C "${REPO_DIR}" rev-parse --verify --quiet "refs/tags/${target}" >/dev/null; then
    printf '%s' "${target}"
    return
  fi

  remote="$(deploy_remote_name)"
  if [[ -n "${remote}" ]] && git -C "${REPO_DIR}" rev-parse --verify --quiet "refs/remotes/${remote}/${target}" >/dev/null; then
    printf '%s' "${remote}/${target}"
    return
  fi

  printf '%s' "${target}"
}

venv_python() {
  printf '%s' "${VENV_DIR}/bin/python"
}

acquire_lock() {
  local lock_file="$1"
  mkdir -p "${LOCK_DIR}"
  exec 9>"${lock_file}"
  flock -n 9 || die "Another operation is running (lock: ${lock_file})."
}
