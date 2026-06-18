#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

ALLOW_DIRTY=0
TARGET_REF=""
ROLLBACK=0

usage() {
  cat <<'EOF'
Usage: update.sh [options]

Options:
  --ref <tag|commit|branch>  Update to target ref.
  --rollback                 Rollback to previously recorded ref.
  --allow-dirty              Allow update while working tree is dirty.
  -h, --help                 Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref) TARGET_REF="${2:-}"; shift 2 ;;
    --rollback) ROLLBACK=1; shift ;;
    --allow-dirty) ALLOW_DIRTY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ -d "${REPO_DIR}/.git" ]] || die "Repository not found at ${REPO_DIR}."
[[ -f "${ENV_FILE}" ]] || die "Missing env file ${ENV_FILE}."
acquire_lock "${UPDATE_LOCK_FILE}"
validate_security_contracts

current_branch="$(git -C "${REPO_DIR}" symbolic-ref --short -q HEAD || true)"
current_ref="$(git_ref)"
prior_previous_ref="$(cat "${DEPLOY_META_DIR}/previous_ref" 2>/dev/null || true)"

if [[ "${ROLLBACK}" == "1" ]]; then
  [[ -n "${prior_previous_ref}" ]] || die "No previous ref recorded for rollback."
  TARGET_REF="${prior_previous_ref}"
fi
[[ -n "${TARGET_REF}" ]] || die "Provide --ref or --rollback."

if [[ "${ALLOW_DIRTY}" != "1" ]] && [[ -n "$(git -C "${REPO_DIR}" status --porcelain)" ]]; then
  die "Working tree is dirty. Re-run with --allow-dirty if intentional."
fi

fetch_deploy_remote
resolved_ref="$(resolve_checkout_ref "${TARGET_REF}")"
target_sha="$(git -C "${REPO_DIR}" rev-parse --verify "${resolved_ref}^{commit}")"

previous_main_ref="$(git -C "${REPO_DIR}" rev-parse --verify refs/heads/main 2>/dev/null || true)"
if [[ -n "${previous_main_ref}" ]]; then
  git -C "${REPO_DIR}" checkout main
  git -C "${REPO_DIR}" reset --hard "${target_sha}"
else
  git -C "${REPO_DIR}" checkout -B main "${target_sha}"
fi

"$(venv_python)" -m pip install -r "${REPO_DIR}/requirements.txt"
systemctl --user restart "${SERVICE_NAME}"

port="$(effective_server_port)"
if ! wait_for_health "${port}" 40 2; then
  warn "Health check failed after update. Attempting rollback."
  if [[ -n "${previous_main_ref}" ]]; then
    git -C "${REPO_DIR}" reset --hard "${previous_main_ref}"
    "$(venv_python)" -m pip install -r "${REPO_DIR}/requirements.txt"
    systemctl --user restart "${SERVICE_NAME}"
    if ! wait_for_health "${port}" 30 2; then
      printf '%s\n' "${TARGET_REF}" > "${DEPLOY_META_DIR}/last_failed_ref"
      die "Rollback failed. Service unhealthy."
    fi
    printf '%s\n' "${TARGET_REF}" > "${DEPLOY_META_DIR}/last_failed_ref"
    record_ref_metadata "${previous_main_ref}" "${prior_previous_ref}"
    die "Update failed, rolled back to previous ref."
  fi
  die "Update failed and no rollback ref available."
fi

new_ref="$(git -C "${REPO_DIR}" rev-parse --short=12 main)"
record_ref_metadata "${new_ref}" "${current_ref}"

echo "[DONE] Deployment updated.
Local main ref: ${new_ref}
Current checkout before update: ${current_branch:-detached}@${current_ref}"