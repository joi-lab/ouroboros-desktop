#!/usr/bin/env bash
# Capture a SWE-bench Pro model_patch from the task repository.
#
# Patch capture determines what the official evaluator sees. A plain
# `git diff BASE` misses new untracked files, while an unfiltered `git add -A`
# captures runtime junk such as Redis dumps, node_modules, and compiled
# binaries. This helper follows the SWE-agent/mini-swe-agent reference shape,
# then removes environment artifacts and binary blobs.
#
# Usage:
#   ./capture_patch.sh <REPO_DIR> <BASE_COMMIT> <OUT.diff> [BASE_UNTRACKED_NUL]
#
# The agent is expected to have already edited <REPO_DIR>. <BASE_COMMIT> is the
# task base commit from the dataset.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd -P)"
WORK="${1:?usage: capture_patch.sh <REPO_DIR> <BASE_COMMIT> <OUT.diff>}"
BASE="${2:?base_commit is required}"
OUT="${3:?output path is required and must be outside the Ouroboros repo}"
BASE_UNTRACKED_SNAPSHOT="${4:-}"
OUT_ABS="$(python3 - "$OUT" <<'PY'
import pathlib
import sys

print(pathlib.Path(sys.argv[1]).expanduser().resolve(strict=False))
PY
)"
case "$OUT_ABS" in
  "$REPO_ROOT"|"$REPO_ROOT"/*)
    echo "output path must be outside the Ouroboros repo: $OUT_ABS" >&2
    exit 2
    ;;
esac
OUT_DIR_ABS="$(dirname "$OUT_ABS")"
mkdir -p "$OUT_DIR_ABS"
STATUS_OUT="${OUT_ABS%.diff}.status.txt"
POST_STATUS_OUT="${OUT_ABS%.diff}.status.post.txt"

git -C "$WORK" rev-parse --verify "$BASE^{commit}" >/dev/null
cleanup() {
  git -C "$WORK" reset -q >/dev/null 2>&1 || true
}
trap cleanup EXIT

# (1) Include newly created source files. Several real Pro fixes add files, and
# a clean `git diff BASE` would omit them.
git -C "$WORK" add -A

# Keep a status snapshot for mismatch debugging: M=modified, A=added,
# ??=untracked.
git -C "$WORK" status --porcelain >"$STATUS_OUT"

# (1b) Drop files that were already untracked at the task base. They are task
# image fixtures, not model-created files, and `git add -A` would otherwise
# leak them into the official model_patch. Keep the file on disk; unstage only.
if [ -n "$BASE_UNTRACKED_SNAPSHOT" ] && [ -s "$BASE_UNTRACKED_SNAPSHOT" ]; then
  if git -C "$WORK" reset -q --pathspec-from-file="$BASE_UNTRACKED_SNAPSHOT" --pathspec-file-nul 2>/dev/null; then
    :
  else
    xargs -0 git -C "$WORK" reset -q -- < "$BASE_UNTRACKED_SNAPSHOT" 2>/dev/null || true
  fi
fi

# (2) Drop environment artifacts. These patterns were chosen to avoid broad
# SWE-agent defaults such as *.cfg/*.toml/setup.py/*.lock, which can remove real
# Pro fixes.
JUNK_RE='appendonlydir|\.rdb$|\.aof$|\.manifest$|\.log$|\.tmp$|\.pid$|\.sock$|(^|/)node_modules/|__pycache__|\.pyc$|\.pyo$|\.pytest_cache|\.ruff_cache|\.mypy_cache|/\.cache/|(^|/)dist/|(^|/)build/|\.DS_Store|(^|/)\.coverage$|coverage\.xml$|/htmlcov/'
while IFS= read -r f; do
  git -C "$WORK" reset -q -- "$f" 2>/dev/null
done < <(git -C "$WORK" diff --cached --name-only "$BASE" | grep -E "$JUNK_RE" || true)

# (3) Drop binary blobs. `git diff --cached --numstat` prints
# "-\t-\t<file>" for binary files. Text source additions remain included.
git -C "$WORK" diff --cached --numstat "$BASE" | awk -F'\t' '$1=="-" && $2=="-" {print $3}' | while IFS= read -r f; do
  [ -n "$f" ] && git -C "$WORK" reset -q -- "$f" 2>/dev/null
done

# (4) Drop incidental lockfile-only side effects when source/code changes are also
# present. A pure lockfile patch is preserved: some ecosystems legitimately treat
# the lockfile as the primary fix. When code changed too, a lockfile whose sibling
# manifest did not change is treated as installer/tooling churn.
python3 - "$WORK" "$BASE" <<'PY' | while IFS= read -r f; do
import pathlib
import subprocess
import sys

work = pathlib.Path(sys.argv[1])
base = sys.argv[2]
proc = subprocess.run(
    ["git", "-C", str(work), "diff", "--cached", "--name-only", base],
    capture_output=True,
    text=True,
    check=False,
)
changed = {line.strip() for line in proc.stdout.splitlines() if line.strip()}

def manifest_for(path: str) -> str:
    p = pathlib.PurePosixPath(path)
    name = p.name
    mapping = {
        "package-lock.json": "package.json",
        "npm-shrinkwrap.json": "package.json",
        "yarn.lock": "package.json",
        "pnpm-lock.yaml": "package.json",
        "go.sum": "go.mod",
        "Cargo.lock": "Cargo.toml",
        "poetry.lock": "pyproject.toml",
        "Pipfile.lock": "Pipfile",
        "composer.lock": "composer.json",
        "Gemfile.lock": "Gemfile",
    }
    manifest = mapping.get(name)
    return str(p.with_name(manifest)) if manifest else ""

lock_to_manifest = {path: manifest_for(path) for path in changed}
lock_to_manifest = {path: manifest for path, manifest in lock_to_manifest.items() if manifest}
if not lock_to_manifest:
    raise SystemExit(0)
non_lock_changes = changed - set(lock_to_manifest)
if not non_lock_changes:
    raise SystemExit(0)
for path, manifest in sorted(lock_to_manifest.items()):
    if manifest not in changed:
        print(path)
PY
  [ -n "$f" ] && git -C "$WORK" reset -q -- "$f" 2>/dev/null
done

# (5) Emit final model_patch and restore the index without touching the working
# tree.
git -C "$WORK" diff --cached --name-status "$BASE" >"$POST_STATUS_OUT"
git -C "$WORK" diff --cached --binary "$BASE" >"$OUT_ABS"

echo "patch -> $OUT_ABS ($(wc -c <"$OUT_ABS" 2>/dev/null || echo 0)B, files: $(grep -cE '^diff --git' "$OUT_ABS" 2>/dev/null || echo 0))" >&2
