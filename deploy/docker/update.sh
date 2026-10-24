#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

ALLOW_DIRTY=0
FORCE_MAIN_RESET=0
TARGET_REF=""
ROLLBACK=0

usage() {
  cat <<'EOF'
Usage: update.sh [options]

Options:
  --ref <tag|commit|branch>  Sync local main to explicit target ref.
  --rollback                 Sync local main to previously recorded ref.
  --allow-dirty              Allow update while working tree is dirty (only relevant when current branch is main).
  --force-main-reset         Force-reset local main when fast-forward is impossible.
  -h, --help                 Show this help.
EOF
}

prompt_main_conflict_resolution() {
  local reason="$1"
  if [[ ! -t 0 ]]; then
    die "${reason}. Non-interactive shell: rerun with --force-main-reset or resolve manually."
  fi

  printf '\n[WARN] %s\n' "${reason}"
  printf 'Choose action for local branch main:\n'
  printf '  1) abort\n'
  printf '  2) hard reset main to target ref\n'

  while true; do
    read -r -p "Enter 1 or 2: " choice
    case "${choice}" in
      1) return 1 ;;
      2) return 0 ;;
      *) echo "Invalid choice. Enter 1 or 2." ;;
    esac
  done
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref) TARGET_REF="${2:-}"; shift 2 ;;
    --rollback) ROLLBACK=1; shift ;;
    --allow-dirty) ALLOW_DIRTY=1; shift ;;
    --force-main-reset) FORCE_MAIN_RESET=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

require_root
validate_compose_runtime
[[ -d "${REPO_DIR}/.git" ]] || die "Repository not found at ${REPO_DIR}."
[[ -f "${ENV_FILE}" ]] || die "Missing env file ${ENV_FILE}."
validate_security_contracts

current_branch="$(git -C "${REPO_DIR}" symbolic-ref --short -q HEAD || true)"
current_ref="$(git_ref)"
prior_previous_ref="$(cat "${DEPLOY_META_DIR}/previous_ref" 2>/dev/null || true)"

if [[ "${ROLLBACK}" == "1" ]]; then
  [[ -n "${prior_previous_ref}" ]] || die "No previous ref recorded for rollback."
  TARGET_REF="${prior_previous_ref}"
  info "Rollback requested. Target ref: ${TARGET_REF}"
fi

[[ -n "${TARGET_REF}" ]] || die "Provide --ref or --rollback."

fetch_deploy_remote
resolved_ref="$(resolve_checkout_ref "${TARGET_REF}")"
target_sha="$(git -C "${REPO_DIR}" rev-parse --verify "${resolved_ref}^{commit}")"

main_exists=0
if git -C "${REPO_DIR}" show-ref --verify --quiet refs/heads/main; then
  main_exists=1
fi

previous_main_ref=""
if [[ "${main_exists}" == "1" ]]; then
  previous_main_ref="$(git -C "${REPO_DIR}" rev-parse --verify refs/heads/main)"
fi

apply_force_reset=0

if [[ "${current_branch}" == "main" ]]; then
  if [[ "${ALLOW_DIRTY}" != "1" ]] && [[ -n "$(git -C "${REPO_DIR}" status --porcelain)" ]]; then
    if [[ "${FORCE_MAIN_RESET}" == "1" ]]; then
      apply_force_reset=1
    else
      if prompt_main_conflict_resolution "main has uncommitted changes"; then
        apply_force_reset=1
      else
        die "Aborted by user."
      fi
    fi
  fi
fi

if [[ "${main_exists}" == "1" ]] && ! git -C "${REPO_DIR}" merge-base --is-ancestor refs/heads/main "${target_sha}"; then
  if [[ "${FORCE_MAIN_RESET}" == "1" ]]; then
    apply_force_reset=1
  else
    if prompt_main_conflict_resolution "main is not ancestor of target ref (fast-forward impossible)"; then
      apply_force_reset=1
    else
      die "Aborted by user."
    fi
  fi
fi

if [[ "${main_exists}" != "1" ]]; then
  info "Local main does not exist. Creating main at ${target_sha}."
  git -C "${REPO_DIR}" branch main "${target_sha}"
else
  if [[ "${apply_force_reset}" == "1" ]]; then
    info "Resetting local main to ${target_sha}."
    if [[ "${current_branch}" == "main" ]]; then
      git -C "${REPO_DIR}" reset --hard "${target_sha}"
    else
      git -C "${REPO_DIR}" branch -f main "${target_sha}"
    fi
  else
    info "Fast-forwarding local main to ${target_sha}."
    if [[ "${current_branch}" == "main" ]]; then
      git -C "${REPO_DIR}" merge --ff-only "${target_sha}"
    else
      git -C "${REPO_DIR}" branch -f main "${target_sha}"
    fi
  fi
fi

new_ref="$(git -C "${REPO_DIR}" rev-parse --verify refs/heads/main)"

info "Applying update (repo bind-mount mode; container restart without image rebuild)."
compose up -d

port="$(effective_server_port)"
if ! wait_for_health "${port}" 30 2; then
  warn "Health check failed after update. Attempting rollback of local main."

  if [[ -n "${previous_main_ref}" ]]; then
    if [[ "${current_branch}" == "main" ]]; then
      git -C "${REPO_DIR}" reset --hard "${previous_main_ref}"
    else
      git -C "${REPO_DIR}" branch -f main "${previous_main_ref}"
    fi
  fi

  compose up -d

  rollback_port="$(effective_server_port)"
  if ! wait_for_health "${rollback_port}" 30 2; then
    printf '%s\n' "${TARGET_REF}" > "${DEPLOY_META_DIR}/last_failed_ref"
    die "Rollback recovery failed. Service still unhealthy. Failed ref: ${TARGET_REF}"
  fi

  printf '%s\n' "${TARGET_REF}" > "${DEPLOY_META_DIR}/last_failed_ref"
  if [[ -n "${previous_main_ref}" ]]; then
    record_ref_metadata "${previous_main_ref}" "${prior_previous_ref}"
  fi
  die "Update to ${TARGET_REF} failed (health check). main rolled back. See ${DEPLOY_META_DIR}/last_failed_ref."
fi

next_previous_ref="${previous_main_ref}"
if [[ "${ROLLBACK}" == "1" ]]; then
  next_previous_ref="${prior_previous_ref}"
fi
record_ref_metadata "${new_ref}" "${next_previous_ref}"

cat <<EOF
[DONE] Deployment updated.
Local main ref: ${new_ref}
Current checkout: ${current_branch:-detached}@${current_ref}
Previous ref marker: ${next_previous_ref:-<unset>}
Timestamp: $(cat "${DEPLOY_META_DIR}/last_deploy_utc")
EOF
