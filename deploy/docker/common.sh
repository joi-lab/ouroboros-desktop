#!/usr/bin/env bash
set -euo pipefail

APP_NAME="ouroboros-headless"
REPO_DIR="/opt/ouroboros-headless"
ENV_DIR="/etc/ouroboros-headless"
ENV_FILE="${ENV_DIR}/.env"
DATA_DIR="/var/lib/ouroboros-headless"
STATE_DIR="${DATA_DIR}/data"
WORKSPACE_DIR="${DATA_DIR}/workspace"
BACKUP_DIR="/var/backups/ouroboros-headless"
DEPLOY_META_DIR="${REPO_DIR}/.deploy"
COMPOSE_FILE="${REPO_DIR}/deploy/docker/docker-compose.vps.yml"
SYSTEMD_UNIT="${APP_NAME}.service"
SEED_REMOTE_NAME="seed"

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
  local cmd="$1"
  command -v "${cmd}" >/dev/null 2>&1 || die "Missing command: ${cmd}"
}

require_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    die "Run as root (sudo)."
  fi
}

ensure_dirs() {
  mkdir -p "${ENV_DIR}" "${DATA_DIR}" "${STATE_DIR}" "${WORKSPACE_DIR}" "${BACKUP_DIR}"
}

ensure_deploy_meta_dir() {
  mkdir -p "${DEPLOY_META_DIR}"
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
    warn "Non-Ubuntu host detected; this is unsupported for MVP. Continuing because override is enabled."
    return
  fi
  die "Unsupported OS for MVP. Use --allow-non-ubuntu to continue at your own risk."
}

validate_compose_runtime() {
  require_cmd docker
  docker info >/dev/null 2>&1 || die "Docker daemon is not reachable."
  docker compose version >/dev/null 2>&1 || die "Docker Compose plugin is required."
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

require_env_key() {
  local key="$1"
  local value
  value="$(read_env_value "${key}")"
  [[ -n "${value}" ]] || die "Required env key is empty: ${key}"
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
        # skip duplicates
        continue
    out.append(line)

if not updated:
    if out and out[-1] != "":
        out.append("")
    out.append(f"{key}={value}")

path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
}

validate_security_contracts() {
  local host_bind legacy_host password fb_root
  host_bind="$(read_env_value "OUROBOROS_HOST_BIND")"
  legacy_host="$(read_env_value "OUROBOROS_SERVER_HOST")"
  password="$(read_env_value "OUROBOROS_NETWORK_PASSWORD")"
  fb_root="$(read_env_value "OUROBOROS_FILE_BROWSER_DEFAULT")"
  if [[ -z "${host_bind}" ]]; then
    host_bind="${legacy_host}"
  fi
  [[ -n "${host_bind}" ]] || host_bind="127.0.0.1"
  [[ -n "${fb_root}" ]] || die "OUROBOROS_FILE_BROWSER_DEFAULT must be set."

  case "${fb_root}" in
    /var/lib/ouroboros-headless/data|/var/lib/ouroboros-headless/data/*|/var/lib/ouroboros-headless/workspace|/var/lib/ouroboros-headless/workspace/*) ;;
    *)
      warn "OUROBOROS_FILE_BROWSER_DEFAULT is outside canonical mounted roots; ensure path is reachable inside container: ${fb_root}"
      ;;
  esac

  if [[ "${host_bind}" != "127.0.0.1" && "${host_bind}" != "localhost" && "${host_bind}" != "::1" ]]; then
    [[ -n "${password}" ]] || die "OUROBOROS_NETWORK_PASSWORD is mandatory for non-loopback bind."
  fi
}

compose() {
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" "$@"
}

server_port() {
  local port
  port="$(read_env_value "OUROBOROS_SERVER_PORT")"
  [[ -n "${port}" ]] || port="8765"
  printf '%s' "${port}"
}

effective_server_port() {
  local runtime_port_file port
  runtime_port_file="${DATA_DIR}/data/state/server_port"
  if [[ -f "${runtime_port_file}" ]]; then
    port="$(tr -d '[:space:]' < "${runtime_port_file}" || true)"
    if [[ "${port}" =~ ^[0-9]+$ ]]; then
      printf '%s' "${port}"
      return
    fi
  fi
  server_port
}

wait_for_health() {
  local port attempts sleep_seconds i
  port="${1:-$(server_port)}"
  attempts="${2:-20}"
  sleep_seconds="${3:-2}"

  for ((i = 1; i <= attempts; i++)); do
    if curl -fsS "http://127.0.0.1:${port}/api/health" >/dev/null 2>&1; then
      return 0
    fi
    if docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' ouroboros-headless 2>/dev/null | grep -q '^healthy$'; then
      return 0
    fi
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
      info "Renamed origin -> ${SEED_REMOTE_NAME} (seed upstream remote)."
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
  [[ -n "${remote}" ]] || die "No git remote configured for deployment updates (expected ${SEED_REMOTE_NAME} or origin)."
  git -C "${REPO_DIR}" fetch "${remote}" --tags
}

resolve_checkout_ref() {
  local target="$1"
  local remote

  [[ -n "${target}" ]] || die "resolve_checkout_ref requires a target ref"

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
