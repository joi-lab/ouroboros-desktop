#!/usr/bin/env bash
# Strip FUTURE git history from a SWE-bench Pro task repository so the gold solution
# cannot be recovered via `git show <fix>` / `git log --all` / tags. SWE-bench Pro
# issue #93 ("Git Reward Hacking") is OPEN/unpatched: the public jefzda/sweap-images
# carry future commits/branches/tags. This keeps ONLY base_commit reachable, as a
# detached HEAD that capture_patch.sh still diffs against.
#
# Warn-only by contract: it never fails the run on residual reachability — it prints
# a WARN line so the operator can see it, and exits 0.
#
# Usage:  strip_gold_history.sh <REPO_DIR> <BASE_COMMIT>
set -uo pipefail
WORK="${1:?usage: strip_gold_history.sh <REPO_DIR> <BASE_COMMIT>}"
BASE="${2:?base_commit is required}"

if ! git -C "$WORK" rev-parse --verify "${BASE}^{commit}" >/dev/null 2>&1; then
  echo "[pro] strip-gold-history: base $BASE is not a commit in $WORK; skipping" >&2
  exit 0
fi

# Detach at base, then delete every other ref (branches/remotes/tags may point at or
# beyond the fix commit), drop remotes, expire the reflog, and gc-prune unreachable
# objects so even a known future sha cannot be `git show`n.
git -C "$WORK" -c advice.detachedHead=false checkout -q --detach "$BASE" 2>/dev/null || true
git -C "$WORK" for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags 2>/dev/null \
  | while read -r ref; do git -C "$WORK" update-ref -d "$ref" 2>/dev/null || true; done
for r in $(git -C "$WORK" remote 2>/dev/null); do
  git -C "$WORK" remote remove "$r" 2>/dev/null || true
done
git -C "$WORK" reflog expire --expire=now --all 2>/dev/null || true
git -C "$WORK" gc --prune=now 2>/dev/null || true

# Verify (warn-only): any commit reachable from a surviving ref that is NOT an
# ancestor of base means the gold history may still be reachable.
LEAK="$(git -C "$WORK" rev-list --all --not "$BASE" 2>/dev/null | head -1)"
if [ -n "$LEAK" ]; then
  echo "[pro] WARN GOLD_HISTORY_REACHABLE: ref-reachable $LEAK is not an ancestor of base after strip (issue #93)" >&2
else
  echo "[pro] gold-history stripped: no ref reaches beyond base $BASE" >&2
fi
exit 0
