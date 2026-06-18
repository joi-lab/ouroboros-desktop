#!/usr/bin/env bash
set -euo pipefail

REF=""
REPO_URL="https://github.com/joi-lab/ouroboros-desktop.git"
ASSET_BASE=""
ALLOW_NON_UBUNTU=0
WITH_BROWSER=""
NON_INTERACTIVE=0

SCRIPT_DIR=""
SCRIPT_PATH="${BASH_SOURCE[0]:-}"
if [[ -n "${SCRIPT_PATH}" && -f "${SCRIPT_PATH}" ]]; then
  SCRIPT_DIR="$(cd -- "$(dirname -- "${SCRIPT_PATH}")" >/dev/null 2>&1 && pwd)"
fi

derive_asset_base() {
  local repo_url="$1" ref="$2" cleaned owner_repo
  cleaned="${repo_url%.git}"
  case "${cleaned}" in
    https://github.com/*)
      owner_repo="${cleaned#https://github.com/}"
      ;;
    git@github.com:*)
      owner_repo="${cleaned#git@github.com:}"
      ;;
    *)
      return 1
      ;;
  esac
  printf 'https://raw.githubusercontent.com/%s/%s/deploy/docker' "${owner_repo}" "${ref}"
}

# When install.sh runs via curl|bash, common.sh is not local yet.
if [[ -z "${SCRIPT_DIR}" || ! -f "${SCRIPT_DIR}/common.sh" ]]; then
  bootstrap_ref="main"
  bootstrap_repo_url="${REPO_URL}"
  bootstrap_asset_base=""

  args=("$@")
  i=0
  while (( i < ${#args[@]} )); do
    case "${args[i]}" in
      --ref)
        if (( i + 1 < ${#args[@]} )); then
          bootstrap_ref="${args[i+1]}"
        fi
        ((i+=2))
        ;;
      --repo-url)
        if (( i + 1 < ${#args[@]} )); then
          bootstrap_repo_url="${args[i+1]}"
        fi
        ((i+=2))
        ;;
      --asset-base)
        if (( i + 1 < ${#args[@]} )); then
          bootstrap_asset_base="${args[i+1]}"
        fi
        ((i+=2))
        ;;
      *)
        ((i+=1))
        ;;
    esac
  done

  if [[ -z "${bootstrap_ref}" ]]; then
    bootstrap_ref="main"
  fi

  if ! command -v curl >/dev/null 2>&1; then
    echo "install.sh requires curl to fetch common.sh when running remotely." >&2
    exit 1
  fi

  if [[ -z "${bootstrap_asset_base}" ]]; then
    if ! bootstrap_asset_base="$(derive_asset_base "${bootstrap_repo_url}" "${bootstrap_ref}")"; then
      bootstrap_asset_base="https://raw.githubusercontent.com/joi-lab/ouroboros-desktop/${bootstrap_ref}/deploy/docker"
    fi
  fi

  tmp_common_dir="$(mktemp -d)"
  cleanup_tmp_common() {
    rm -rf "${tmp_common_dir}"
  }
  trap cleanup_tmp_common EXIT

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
  --with-browser             Build VPS image with browser tooling enabled.
  --non-interactive          Skip prompts and reuse defaults/current .env values.
  -h, --help                 Show this help.
EOF
}

is_loopback_bind() {
  case "$1" in
    127.0.0.1|localhost|::1) return 0 ;;
    *) return 1 ;;
  esac
}

prompt_with_default() {
  local label="$1"
  local default_value="$2"
  local value=""
  read -r -p "${label} [${default_value}]: " value < /dev/tty || true
  if [[ -z "${value}" ]]; then
    value="${default_value}"
  fi
  printf '%s' "${value}"
}

prompt_yes_no() {
  local label="$1"
  local default_yes_no="$2"
  local hint="[y/N]"
  local value=""

  if [[ "${default_yes_no}" == "1" ]]; then
    hint="[Y/n]"
  fi

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

prompt_port() {
  local default_port="$1"
  local value=""
  while true; do
    value="$(prompt_with_default "OUROBOROS_SERVER_PORT" "${default_port}")"
    if [[ "${value}" =~ ^[0-9]+$ ]] && (( value >= 1 && value <= 65535 )); then
      printf '%s' "${value}"
      return
    fi
    warn "Port must be an integer in range 1..65535."
  done
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref) REF="${2:-}"; shift 2 ;;
    --repo-url) REPO_URL="${2:-}"; shift 2 ;;
    --asset-base) ASSET_BASE="${2:-}"; shift 2 ;;
    --allow-non-ubuntu) ALLOW_NON_UBUNTU=1; shift ;;
    --with-browser) WITH_BROWSER=1; shift ;;
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

if [[ -n "${ASSET_BASE}" ]]; then
  info "Using custom --asset-base only for remote common bootstrap: ${ASSET_BASE}"
fi

INSTALL_STATE_FILE="${DEPLOY_META_DIR}/install-state.env"

ensure_install_state_file() {
  ensure_deploy_meta_dir
  touch "${INSTALL_STATE_FILE}"
  chmod 600 "${INSTALL_STATE_FILE}" || true
}

install_state_get() {
  local key="$1"
  [[ -f "${INSTALL_STATE_FILE}" ]] || { printf ''; return; }
  awk -F= -v k="${key}" '$1==k {v=substr($0, index($0,"=")+1)} END {printf "%s", v}' "${INSTALL_STATE_FILE}"
}

install_state_set() {
  local key="$1"
  local value="$2"
  local tmp

  ensure_install_state_file
  tmp="$(mktemp)"
  awk -F= -v k="${key}" -v v="${value}" '
    BEGIN { updated=0 }
    {
      if ($1 == k) {
        if (!updated) {
          print k "=" v
          updated=1
        }
        next
      }
      print $0
    }
    END {
      if (!updated) print k "=" v
    }
  ' "${INSTALL_STATE_FILE}" > "${tmp}"
  mv "${tmp}" "${INSTALL_STATE_FILE}"
  chmod 600 "${INSTALL_STATE_FILE}" || true
}

stage_enter() {
  local stage_key="$1"
  local title="$2"
  info ""
  info "== [stage:${stage_key}] ${title} =="
  install_state_set "INSTALL_STAGE" "${stage_key}"
  install_state_set "INSTALL_UPDATED_AT" "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
}

stage_skip() {
  local stage_key="$1"
  local title="$2"
  info "[stage:${stage_key}] already completed earlier; skipping (${title})."
}

mark_stage_done() {
  local index="$1"
  local stage_key="$2"
  install_state_set "INSTALL_STAGE_INDEX_DONE" "${index}"
  install_state_set "INSTALL_LAST_COMPLETED_STAGE" "${stage_key}"
  done_index="${index}"
}

apt_updated=0
apt_install() {
  if [[ "${apt_updated}" == "0" ]]; then
    apt-get update -y
    apt_updated=1
  fi
  apt-get install -y "$@"
}

require_root
validate_ubuntu_or_override "${ALLOW_NON_UBUNTU}"
ensure_install_state_file

previous_status="$(install_state_get "INSTALL_STATUS")"
previous_target_ref="$(install_state_get "INSTALL_TARGET_REF")"
done_index_raw="$(install_state_get "INSTALL_STAGE_INDEX_DONE")"

resume_mode=0
if [[ "${previous_status}" == "in_progress" ]]; then
  resume_mode=1
fi

if [[ "${resume_mode}" == "1" ]]; then
  if [[ -z "${REF}" && -n "${previous_target_ref}" ]]; then
    REF="${previous_target_ref}"
  fi
  if [[ -n "${previous_target_ref}" && -n "${REF}" && "${REF}" != "${previous_target_ref}" ]]; then
    die "Install resume is pinned to ref ${previous_target_ref}. Re-run with --ref ${previous_target_ref} or remove ${INSTALL_STATE_FILE}."
  fi
  info "Resume mode: continuing interrupted install${REF:+ (target ref: ${REF})}."
else
  done_index_raw="0"
  install_state_set "INSTALL_STARTED_AT" "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  install_state_set "INSTALL_STAGE_INDEX_DONE" "0"
fi

if [[ -z "${REF}" ]]; then
  REF="main"
fi

if [[ ! "${done_index_raw}" =~ ^[0-9]+$ ]]; then
  done_index_raw="0"
fi
done_index="${done_index_raw}"

install_state_set "INSTALL_STATUS" "in_progress"
install_state_set "INSTALL_TARGET_REF" "${REF}"
install_state_set "INSTALL_REPO_URL" "${REPO_URL}"

if (( done_index >= 1 )); then
  stage_skip "prereqs" "install prerequisites"
else
  stage_enter "prereqs" "install prerequisites"
  info "Installing prerequisites (git + gh + docker if needed)."

  if ! command -v curl >/dev/null 2>&1; then
    apt_install curl
  fi
  if ! command -v git >/dev/null 2>&1; then
    apt_install git
  fi
  if ! command -v gh >/dev/null 2>&1; then
    apt_install gh
  fi

  if ! command -v docker >/dev/null 2>&1; then
    apt_install ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings

    if [[ ! -f /etc/apt/keyrings/docker.gpg ]]; then
      curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
      chmod a+r /etc/apt/keyrings/docker.gpg
    fi

    # shellcheck disable=SC1091
    source /etc/os-release
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list

    apt-get update -y
    apt_updated=1
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  fi

  if ! docker compose version >/dev/null 2>&1; then
    info "Installing missing Docker Compose plugin."
    apt_install docker-compose-plugin
  fi

  validate_compose_runtime
  mark_stage_done 1 "prereqs"
fi

ensure_dirs
chmod 700 "${ENV_DIR}" || true
chmod 700 "${DATA_DIR}" || true

if (( done_index >= 2 )); then
  stage_skip "repo" "sync runtime repository and checkout target ref"
else
  stage_enter "repo" "sync runtime repository and checkout target ref"

  if [[ -d "${REPO_DIR}/.git" ]]; then
    info "Repository already exists at ${REPO_DIR}; refreshing deploy remote."
  elif [[ -d "${REPO_DIR}" ]]; then
    if [[ -z "$(ls -A "${REPO_DIR}" 2>/dev/null || true)" ]]; then
      info "Repository directory exists but is empty; cloning into ${REPO_DIR}"
      git clone "${REPO_URL}" "${REPO_DIR}"
    else
      backup_repo_dir="${REPO_DIR}.preinstall.$(date -u +%Y%m%d-%H%M%S)"
      warn "${REPO_DIR} exists and is not a git checkout. Auto-backup -> ${backup_repo_dir}"
      mv "${REPO_DIR}" "${backup_repo_dir}"
      git clone "${REPO_URL}" "${REPO_DIR}"

      if [[ -d "${backup_repo_dir}/.deploy" ]]; then
        info "Restoring prior deploy metadata from ${backup_repo_dir}/.deploy"
        mkdir -p "${REPO_DIR}/.deploy"
        cp -a "${backup_repo_dir}/.deploy/." "${REPO_DIR}/.deploy/"
      fi
    fi
  else
    info "Cloning repository into ${REPO_DIR}"
    git clone "${REPO_URL}" "${REPO_DIR}"
  fi

  ensure_seed_remote "${REPO_URL}"
  fetch_deploy_remote

  if [[ -n "${REF}" ]]; then
    info "Checking out ${REF}"
    resolved_ref="$(resolve_checkout_ref "${REF}")"
    git -C "${REPO_DIR}" checkout "${resolved_ref}"
  fi

  mark_stage_done 2 "repo"
fi

if (( done_index >= 3 )); then
  stage_skip "env" "prepare deploy environment"
else
  stage_enter "env" "prepare deploy environment"

  if [[ ! -f "${ENV_FILE}" ]]; then
    info "Initializing ${ENV_FILE} from template."
    cp "${REPO_DIR}/deploy/docker/.env.example" "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
    info "Created deploy env file. API and Telegram credentials can be configured later in Dashboard."
  else
    chmod 600 "${ENV_FILE}"
  fi

  browser_profile="$(read_env_value "VPS_ENABLE_BROWSER_TOOLS")"
  if [[ "${browser_profile}" != "1" ]]; then
    browser_profile="0"
  fi
  if [[ -n "${WITH_BROWSER}" ]]; then
    browser_profile="${WITH_BROWSER}"
  fi

  host_bind="$(read_env_value "OUROBOROS_HOST_BIND")"
  if [[ -z "${host_bind}" ]]; then
    host_bind="$(read_env_value "OUROBOROS_SERVER_HOST")"
  fi
  [[ -n "${host_bind}" ]] || host_bind="127.0.0.1"

  server_port="$(read_env_value "OUROBOROS_SERVER_PORT")"
  if [[ ! "${server_port}" =~ ^[0-9]+$ ]]; then
    server_port="8765"
  fi

  file_browser_root="$(read_env_value "OUROBOROS_FILE_BROWSER_DEFAULT")"
  [[ -n "${file_browser_root}" ]] || file_browser_root="/var/lib/ouroboros-headless/workspace"

  network_password="$(read_env_value "OUROBOROS_NETWORK_PASSWORD")"

  interactive=1
  if [[ "${NON_INTERACTIVE}" == "1" || ! -r /dev/tty ]]; then
    interactive=0
  fi
  if [[ "${resume_mode}" == "1" && "${NON_INTERACTIVE}" != "1" ]]; then
    info "Resume mode: reusing current .env values without prompts."
    interactive=0
  fi

  if [[ "${interactive}" == "1" ]]; then
    info "Interactive deploy setup (.env)."

    if [[ -z "${WITH_BROWSER}" ]]; then
      browser_profile="$(prompt_yes_no "Install browser tools in VPS image now" "${browser_profile}")"
    fi

    host_bind="$(prompt_with_default "OUROBOROS_HOST_BIND" "${host_bind}")"
    server_port="$(prompt_port "${server_port}")"
    file_browser_root="$(prompt_with_default "OUROBOROS_FILE_BROWSER_DEFAULT" "${file_browser_root}")"

    if is_loopback_bind "${host_bind}"; then
      info "Loopback bind selected (${host_bind}); network password is optional."
    else
      if [[ -n "${network_password}" ]]; then
        read -r -s -p "OUROBOROS_NETWORK_PASSWORD (required for non-loopback, Enter to keep current): " new_password < /dev/tty || true
        printf '\n'
        if [[ -n "${new_password}" ]]; then
          network_password="${new_password}"
        fi
      else
        while [[ -z "${network_password}" ]]; do
          read -r -s -p "OUROBOROS_NETWORK_PASSWORD (required for non-loopback): " network_password < /dev/tty || true
          printf '\n'
          if [[ -z "${network_password}" ]]; then
            warn "OUROBOROS_NETWORK_PASSWORD cannot be empty for non-loopback bind."
          fi
        done
      fi
    fi
  fi

  if [[ "${interactive}" == "0" ]] && ! is_loopback_bind "${host_bind}" && [[ -z "${network_password}" ]]; then
    die "OUROBOROS_NETWORK_PASSWORD is required for non-loopback bind (set it in ${ENV_FILE} or run install interactively)."
  fi

  set_env_value "VPS_ENABLE_BROWSER_TOOLS" "${browser_profile}"
  set_env_value "OUROBOROS_HOST_BIND" "${host_bind}"
  set_env_value "OUROBOROS_SERVER_PORT" "${server_port}"
  set_env_value "OUROBOROS_FILE_BROWSER_DEFAULT" "${file_browser_root}"
  if [[ -n "${network_password}" ]]; then
    set_env_value "OUROBOROS_NETWORK_PASSWORD" "${network_password}"
  fi

  if [[ "${browser_profile}" == "1" ]]; then
    info "Browser tooling profile: enabled"
  else
    info "Browser tooling profile: disabled (lean default)"
  fi

  validate_security_contracts
  mark_stage_done 3 "env"
fi

if (( done_index >= 4 )); then
  stage_skip "systemd" "install and enable systemd unit"
else
  stage_enter "systemd" "install and enable systemd unit"
  install -m 0644 "${REPO_DIR}/deploy/docker/ouroboros-headless.service" "/etc/systemd/system/${SYSTEMD_UNIT}"
  systemctl daemon-reload
  systemctl enable "${SYSTEMD_UNIT}"
  mark_stage_done 4 "systemd"
fi

if (( done_index >= 5 )); then
  stage_skip "runtime" "build container image and start service"
else
  stage_enter "runtime" "build container image and start service"
  info "Building/updating container image (explicit install-time build)."
  compose up -d --build
  systemctl start "${SYSTEMD_UNIT}"
  mark_stage_done 5 "runtime"
fi

if (( done_index >= 6 )); then
  stage_skip "health" "health validation and deploy metadata"
else
  stage_enter "health" "health validation and deploy metadata"
  port="$(effective_server_port)"
  wait_for_health "${port}" 90 2 || die "Health check failed after install (port ${port})."

  current_ref="$(git_ref)"
  record_ref_metadata "${current_ref}" ""
  mark_stage_done 6 "health"
fi

port="$(effective_server_port)"
current_ref="$(git_ref)"
install_state_set "INSTALL_STATUS" "done"
install_state_set "INSTALL_COMPLETED_AT" "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

cat <<EOF
[DONE] Ouroboros headless installed.

Status:
  systemctl status ${SYSTEMD_UNIT}
Logs:
  journalctl -u ${SYSTEMD_UNIT} -f
  docker compose --env-file ${ENV_FILE} -f ${COMPOSE_FILE} logs -f
Health:
  curl -fsS http://127.0.0.1:${port}/api/health
Tunnel:
  ssh -L ${port}:127.0.0.1:${port} <user>@<vps>
Dashboard setup:
  Open dashboard and configure Telegram/API providers in Settings.
Update:
  sudo ${REPO_DIR}/deploy/docker/update.sh --ref <tag-or-commit>
Enable browser tools later (optional):
  sudo ${REPO_DIR}/deploy/docker/enable-browser-tools.sh
Backup:
  sudo ${REPO_DIR}/deploy/docker/backup.sh
Current ref:
  ${current_ref}
Deploy remote:
  $(deploy_remote_name)
Git ownership:
  seed remote is used for installer updates; origin is reserved for optional user repo sync.
Install state file:
  ${INSTALL_STATE_FILE}
Previous ref marker:
  ${DEPLOY_META_DIR}/previous_ref
EOF
