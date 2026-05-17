"""Git PR integration tools: fetch_pr_ref, create_integration_branch,
cherry_pick_pr_commits, stage_adaptations, stage_pr_merge.

Attribution invariant: `cherry_pick_pr_commits` replays each source commit with
`git cherry-pick --no-edit`, preserving author identity/date while setting the
committer explicitly from repo-local git config (fallback:
`Ouroboros <ouroboros@local.mac>`). `override_author` may amend author
name/email for placeholder-author PRs while preserving the original author date.

Reviewed merge invariant: adaptation edits are staged, not committed, then
`stage_pr_merge` runs `git merge --no-ff --no-commit`; the final reviewed
`repo_commit` creates the single merge commit on `ouroboros`. There is no
reviewed commit path on the integration branch and no unreviewed commit path in
this module.

Packaged-build note: `git_pr.py` is dev/source auto-discovered and not in
`registry.py::_FROZEN_TOOL_MODULES`; `github.py` is frozen so PR inspection
tools remain packaged.
"""

from __future__ import annotations

import logging
import os
import pathlib
import re
import subprocess
from typing import List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.commit_gate import _invalidate_advisory
from ouroboros.tools.git import _acquire_git_lock, _release_git_lock, _sanitize_git_error

log = logging.getLogger(__name__)

_PR_BRANCH_PREFIX = "integrate/pr-"


def _g(args: List[str], cwd: pathlib.Path,
       env: Optional[dict] = None, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a git subprocess; returns CompletedProcess (returncode, stdout, stderr)."""
    return subprocess.run(
        ["git"] + args, cwd=str(cwd),
        capture_output=True, text=True, timeout=timeout,
        **({"env": env} if env is not None else {}),
    )


def _ouroboros_committer_env(repo_dir: pathlib.Path) -> dict:
    """Build an env dict with explicit GIT_COMMITTER_* set to this repo's user identity.

    Reads user.name / user.email from the repo-local git config only (--local).
    Atomic pair fallback: if EITHER local field is missing or empty, BOTH
    fields fall back together to the Ouroboros defaults. This prevents
    mixed/Frankenstein identities like ``Alice <ouroboros@local.mac>`` that
    could occur if only one of the two fields were configured. It also
    ensures we never leak the developer's global git identity into
    cherry-picked commits — committer attribution stays deterministic and
    testable regardless of ambient shell identity.
    """
    env = os.environ.copy()
    name_r = subprocess.run(
        ["git", "config", "--local", "user.name"], cwd=str(repo_dir),
        capture_output=True, text=True, timeout=5,
    )
    email_r = subprocess.run(
        ["git", "config", "--local", "user.email"], cwd=str(repo_dir),
        capture_output=True, text=True, timeout=5,
    )
    local_name = name_r.stdout.strip() if name_r.returncode == 0 else ""
    local_email = email_r.stdout.strip() if email_r.returncode == 0 else ""
    # Atomic pair fallback: if EITHER field is missing or empty, use both
    # Ouroboros defaults. Prevents mixed identities like 'Alice <ouroboros@local.mac>'.
    if local_name and local_email:
        env["GIT_COMMITTER_NAME"] = local_name
        env["GIT_COMMITTER_EMAIL"] = local_email
    else:
        env["GIT_COMMITTER_NAME"] = "Ouroboros"
        env["GIT_COMMITTER_EMAIL"] = "ouroboros@local.mac"
    return env


def _validate_git_ref_arg(value: str, param_name: str) -> Optional[str]:
    """Return an error string if value looks like a git option (starts with '-').

    Prevents option-injection attacks where a caller passes '--abort' or '--all'
    as a remote/branch/ref argument, causing git to interpret it as a flag.
    Returns None when the value is safe.
    """
    if value.startswith("-"):
        return (
            f"⚠️ INVALID_ARG: {param_name!r} must not start with '-' "
            f"(got {value!r}). Option-like values are rejected for safety."
        )
    return None


# Characters forbidden in override_author name/email — these break the
# `--author="Name <email>"` argument parsing or introduce malformed git
# metadata. Note: git author strings are passed as a single argv element
# (not shell-interpolated), so there is no shell-injection risk, but
# angle brackets and control chars still corrupt the format git writes
# into the commit object.
_AUTHOR_FORBIDDEN_CHARS = ("\r", "\n", "\t", "<", ">", "\x00")


# Commit SHA format: 7-40 hex characters. Used to reject symbolic refs
# (branch names, HEAD, HEAD~1, lightweight tag names, etc.) up front before
# any git operation. Commit-SHA-only is the documented contract; `git
# cat-file -t` is insufficient because branches, HEAD, and lightweight tags
# all resolve to 'commit' too.
_SHA_PATTERN = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)


def _validate_override_author(override: Optional[dict]) -> Optional[str]:
    """Validate an override_author dict. Returns None if valid (or None input),
    or an error string starting with '⚠️ CHERRY_PICK_ERROR:' on failure.

    Valid shape: {"name": <non-empty str>, "email": <non-empty str with '@'>}
    Both fields are required; extra keys are ignored (forward-compat).
    Forbidden characters in either field: newline, CR, tab, NUL, '<', '>'.
    """
    if override is None:
        return None
    if not isinstance(override, dict):
        return (
            "⚠️ CHERRY_PICK_ERROR: override_author must be a dict with "
            "'name' and 'email' keys (got "
            f"{type(override).__name__})."
        )
    name = override.get("name")
    email = override.get("email")
    if not isinstance(name, str) or not name.strip():
        return (
            "⚠️ CHERRY_PICK_ERROR: override_author['name'] must be a "
            "non-empty string."
        )
    if not isinstance(email, str) or not email.strip():
        return (
            "⚠️ CHERRY_PICK_ERROR: override_author['email'] must be a "
            "non-empty string."
        )
    if "@" not in email:
        return (
            "⚠️ CHERRY_PICK_ERROR: override_author['email'] must contain '@' "
            f"(got {email!r})."
        )
    for ch in _AUTHOR_FORBIDDEN_CHARS:
        if ch in name or ch in email:
            return (
                "⚠️ CHERRY_PICK_ERROR: override_author contains a forbidden "
                "character (newline, CR, tab, NUL, '<', or '>'). These "
                "would corrupt git commit metadata."
            )
    return None


def _fetch_pr_ref(ctx: ToolContext, pr_number: int, remote: str = "origin") -> str:
    """Fetch a GitHub PR's commits locally via the pull/{n}/head ref.

    Uses the '+' force-update prefix so the fetch succeeds even when the
    PR has been rebased or force-pushed since the last fetch.  After
    fetching, the local ref pr/{pr_number} is available for cherry-pick.

    Author metadata on the fetched commits is preserved exactly as the
    contributor pushed it — no re-authoring occurs.
    """
    if pr_number <= 0:
        return "⚠️ PR_FETCH_ERROR: pr_number must be a positive integer."
    err = _validate_git_ref_arg(remote, "remote")
    if err:
        return f"⚠️ PR_FETCH_ERROR: {err}"

    repo_dir = pathlib.Path(ctx.repo_dir)
    local_ref = f"pr/{pr_number}"
    # '+' prefix allows non-fast-forward updates (rebased / force-pushed PRs)
    refspec = f"+refs/pull/{pr_number}/head:{local_ref}"

    lock = _acquire_git_lock(ctx)
    try:
        result = _g(["fetch", remote, refspec], repo_dir, timeout=120)
        if result.returncode != 0:
            err = (result.stderr or "").strip()
            return f"⚠️ PR_FETCH_ERROR: {_sanitize_git_error(err)}"

        fetched_sha = _g(["rev-parse", local_ref], repo_dir).stdout.strip()

        base_r = _g(["merge-base", "ouroboros", local_ref], repo_dir)
        base_sha = base_r.stdout.strip() if base_r.returncode == 0 else ""
        if base_sha:
            count_r = _g(["rev-list", "--count", f"{base_sha}..{local_ref}"], repo_dir)
            commit_count = count_r.stdout.strip() if count_r.returncode == 0 else "?"
            log_r = _g(["log", "--format=%h | %an <%ae> | %ai | %s",
                        f"{base_sha}..{local_ref}"], repo_dir)
        else:
            commit_count = "?"
            log_r = _g(["log", "--format=%h | %an <%ae> | %ai | %s", local_ref], repo_dir)
    finally:
        _release_git_lock(lock)

    commit_log = (log_r.stdout.strip()
                  if log_r.returncode == 0 else "(could not list commits)")

    return (
        f"✅ Fetched PR #{pr_number} → local ref '{local_ref}'\n"
        f"  HEAD SHA: {fetched_sha[:12]}\n"
        f"  Commits vs ouroboros: {commit_count}\n\n"
        f"Commits (author | date | subject):\n{commit_log}\n\n"
        f"Next step: create_integration_branch(pr_number={pr_number})"
    )


def _create_integration_branch(
    ctx: ToolContext,
    pr_number: int,
    base_branch: str = "ouroboros",
) -> str:
    """Create a fresh integration branch (integrate/pr-N) from base_branch.

    The integration branch is where external commits will be cherry-picked
    (preserving original authorship) and any Ouroboros adaptation changes
    will be staged for finalization through the reviewed pipeline.
    """
    if pr_number <= 0:
        return "⚠️ PR_BRANCH_ERROR: pr_number must be a positive integer."
    err = _validate_git_ref_arg(base_branch, "base_branch")
    if err:
        return f"⚠️ PR_BRANCH_ERROR: {err}"

    repo_dir = pathlib.Path(ctx.repo_dir)
    branch_name = f"{_PR_BRANCH_PREFIX}{pr_number}"

    if _g(["branch", "--list", branch_name], repo_dir).stdout.strip():
        return (
            f"⚠️ PR_BRANCH_ERROR: Branch '{branch_name}' already exists.\n"
            f"To start fresh: git branch -D {branch_name}"
        )

    # Guard: refuse to create a branch if the working tree is not clean.
    # git checkout carries staged/unstaged tracked edits onto the new branch;
    # stage_adaptations runs `git add -A` which would also pick up any pre-existing
    # untracked files, contaminating the integration branch with unrelated work.
    # We check all three states via `git status --porcelain`.
    status_r = _g(["status", "--porcelain"], repo_dir)
    if status_r.returncode == 0 and status_r.stdout.strip():
        return (
            "⚠️ PR_BRANCH_ERROR: Working tree has uncommitted or untracked changes.\n"
            "Commit, stash, or clean before creating an integration branch.\n"
            f"Unclean files:\n{status_r.stdout.strip()[:300]}"
        )

    lock = _acquire_git_lock(ctx)
    try:
        head_r = _g(["rev-parse", "--abbrev-ref", "HEAD"], repo_dir)
        current_branch = head_r.stdout.strip() if head_r.returncode == 0 else "?"

        co = _g(["checkout", base_branch], repo_dir)
        if co.returncode != 0:
            return (
                f"⚠️ PR_BRANCH_ERROR: Cannot checkout '{base_branch}': "
                f"{_sanitize_git_error((co.stderr or '').strip())}"
            )

        br = _g(["checkout", "-b", branch_name], repo_dir)
        if br.returncode != 0:
            return (
                f"⚠️ PR_BRANCH_ERROR: Cannot create branch '{branch_name}': "
                f"{_sanitize_git_error((br.stderr or '').strip())}"
            )

        base_sha = _g(["rev-parse", "HEAD"], repo_dir).stdout.strip()[:12]
    finally:
        _release_git_lock(lock)

    return (
        f"✅ Created integration branch '{branch_name}' from '{base_branch}' ({base_sha})\n"
        f"  (was on: {current_branch})\n\n"
        f"Next steps:\n"
        f"  1. cherry_pick_pr_commits(shas=[...])    ← replays external commits with\n"
        f"                                              original author attribution\n"
        f"  2. stage_adaptations()                   ← optional: stage Ouroboros\n"
        f"                                              adaptation changes (no commit)\n"
        f"  3. stage_pr_merge(branch='{branch_name}') → advisory_pre_review → repo_commit\n"
        f"     (staged adaptations from step 2 land in the final merge commit)"
    )


def _rollback_failed_amend(
    ctx: ToolContext,
    repo_dir: pathlib.Path,
    sha: str,
    amend_r: subprocess.CompletedProcess,
    applied: List[str],
) -> str:
    """Rollback the just-created cherry-pick commit after a failed --amend.

    HEAD~1 is safe here because the caller just created HEAD via cherry-pick
    in the same loop iteration. Removes the sha from the applied list (since
    it's been rolled back), invalidates advisory for any earlier successful
    commits in the batch, and returns a diagnostic error string for the caller
    to return directly.
    """
    amend_err = (amend_r.stderr or amend_r.stdout or "").strip()
    _g(["reset", "--hard", "HEAD~1"], repo_dir)
    applied.pop()
    if applied:
        _invalidate_advisory(
            ctx, changed_paths=[], mutation_root=repo_dir,
            source_tool="cherry_pick_pr_commits",
        )
    return (
        f"⚠️ CHERRY_PICK_ERROR: author amend failed on {sha[:12]} "
        f"(rolled back to pre-commit state):\n{amend_err[:500]}\n\n"
        f"Applied and kept before amend failure: "
        f"{[s for s in applied] or 'none'}\n"
        f"Amend failures indicate a git config or author-string problem, "
        f"not a PR content problem — fail-fast is intentional."
    )


def _validate_sha_list(
    shas: List[str],
    repo_dir: pathlib.Path,
) -> object:
    """Prevalidate a list of refs as commit SHAs.

    Returns either a list of resolved full SHAs (success) or an error string
    starting with '⚠️ CHERRY_PICK_ERROR:' (failure). Helper extracted from
    _cherry_pick_pr_commits to keep that function under the 250-line hard gate.

    Two checks per ref:
      1. `git rev-parse --verify <ref>^{commit}` — ref must resolve to a
         commit object (catches tree/blob objects).
      2. `git cat-file -t <ref>` must equal 'commit' — rejects tag refs that
         dereference to commits (annotated or lightweight tags). Cherry-picking
         a tag is semantically meaningless and the later `%aI` date-read path
         expects a commit SHA exactly.
    """
    resolved: List[str] = []
    for sha in shas:
        sha = sha.strip()
        # Require hex-only SHA format (7-40 chars). This rejects branch names,
        # HEAD, HEAD~1, lightweight tags, and any other symbolic refs up front,
        # before any git operation. Commit-SHA-only is the documented contract.
        if not _SHA_PATTERN.match(sha):
            return (
                f"⚠️ CHERRY_PICK_ERROR: '{sha}' is not a commit SHA "
                f"(expected 7-40 hex characters). Symbolic refs like branch "
                f"names, HEAD, or tag names are not accepted — resolve them "
                f"to a commit SHA first via `git rev-parse` or fetch_pr_ref."
            )
        r = _g(["rev-parse", "--verify", f"{sha}^{{commit}}"], repo_dir)
        if r.returncode != 0:
            return (
                f"⚠️ CHERRY_PICK_ERROR: Cannot resolve SHA '{sha}' to a commit. "
                f"Verify it was fetched with fetch_pr_ref and is a commit object."
            )
        resolved.append(r.stdout.strip())
        # Reject tag refs and non-commit object types.
        type_r = _g(["cat-file", "-t", sha], repo_dir)
        if type_r.returncode != 0 or type_r.stdout.strip() != "commit":
            obj_type = type_r.stdout.strip() or "unknown"
            return (
                f"⚠️ CHERRY_PICK_ERROR: '{sha}' is a {obj_type!r} object, "
                f"not a commit. Only commit SHAs are accepted — tags and other "
                f"refs must be dereferenced first (e.g. by looking up the target "
                f"commit SHA via fetch_pr_ref or git log)."
            )
    return resolved


def _amend_author_on_head(
    repo_dir: pathlib.Path,
    override_author: dict,
    orig_date: str,
    committer_env: dict,
) -> subprocess.CompletedProcess:
    """Run `git commit --amend --no-edit --author=... --date=<orig>` on HEAD.

    Helper extracted from _cherry_pick_pr_commits to keep that function under
    the 250-line hard gate. Rewrites the author of the current HEAD commit
    (assumed to be a just-created cherry-pick) while preserving the original
    author date (passed via --date) and the repo-local committer identity
    with Ouroboros fallback (via committer_env's GIT_COMMITTER_* vars).

    Returns the CompletedProcess so the caller can inspect returncode/stderr
    and decide on rollback. Does NOT handle failure itself — that's the
    caller's responsibility (it needs access to the applied list for
    advisory invalidation).
    """
    author_str = f'{override_author["name"]} <{override_author["email"]}>'
    return _g(
        ["commit", "--amend", "--no-edit",
         f"--author={author_str}",
         f"--date={orig_date}"],
        repo_dir, env=committer_env,
    )


def _cherry_pick_pr_commits(
    ctx: ToolContext,
    shas: List[str],
    stop_on_conflict: bool = True,
    override_author: Optional[dict] = None,
) -> str:
    """Replay PR commits onto the current integration branch.

    ATTRIBUTION CONTRACT (default: override_author=None):
      Uses `git cherry-pick --no-edit` (NOT --no-commit).
      Each PR commit is replayed as a real commit with:
        - author name / email = original contributor (preserved by git)
        - author date         = original timestamp  (preserved by git)
        - committer           = repo-local configured identity (Ouroboros fallback)

      GIT_COMMITTER_NAME and GIT_COMMITTER_EMAIL are injected explicitly
      from the repo's user.name / user.email config so attribution is
      deterministic regardless of ambient shell identity.

      GitHub contribution counting is based on author.email, so external
      contributors receive full graph credit.

    OPTIONAL AUTHOR OVERRIDE (override_author={"name": ..., "email": ...}):
      When supplied, after each successful cherry-pick a second step
      `git commit --amend --no-edit --author="Name <email>" --date=<orig>`
      rewrites the author name+email on the new commit while preserving:
        - the ORIGINAL author date (captured from the source sha via %aI
          BEFORE the cherry-pick and passed to --date)
        - the repo-local committer identity, with Ouroboros fallback
          (via GIT_COMMITTER_* env)
      If the amend step fails, the just-added commit is rolled back with
      `git reset --hard HEAD~1` and the function returns an error with
      context; earlier successfully-amended commits in the same batch are
      kept (advisory invalidation still fires).

      The override applies to the ENTIRE batch uniformly — intended for
      single-author placeholder commit sets (e.g. external contributor
      ran Ouroboros locally without configuring git user.email). Mixed-
      author batches will all be rewritten to the same override identity;
      split the batch by author if that is not desired.

      When override_author is set, the Co-authored-by hint continues to
      reference the ORIGINAL upstream author (read from the source commit's
      `%an <%ae>`), NOT the override identity. Rationale: the override is
      already the canonical author on the amended commit, so putting the
      same identity in Co-authored-by would be redundant and would erase
      the real upstream contributor from the merge-commit message.

    stop_on_conflict=True  (default): abort on first conflict, leave repo clean.
    stop_on_conflict=False: skip conflicting SHAs, continue.
      Skipped SHAs are explicitly reported — partial ingestion is never silent.
      Note: amend failures (override_author path) ALWAYS abort the current
      commit regardless of stop_on_conflict — a failing amend signals a git
      config problem, not a PR content problem.
    """
    # Fail-fast validation of override_author — runs before any git work,
    # so invalid input returns an error without touching the repo.
    override_error = _validate_override_author(override_author)
    if override_error:
        return override_error

    if not shas:
        return "⚠️ CHERRY_PICK_ERROR: shas list cannot be empty."

    repo_dir = pathlib.Path(ctx.repo_dir)

    head_r = _g(["rev-parse", "--abbrev-ref", "HEAD"], repo_dir)
    current_branch = head_r.stdout.strip() if head_r.returncode == 0 else ""
    if not current_branch.startswith(_PR_BRANCH_PREFIX):
        return (
            f"⚠️ CHERRY_PICK_ERROR: Current branch is '{current_branch}', "
            f"not an integration branch (expected prefix '{_PR_BRANCH_PREFIX}').\n"
            f"Run create_integration_branch first."
        )

    # Validate all SHAs before starting — avoids partial application on typo.
    # Rejects non-commit objects and tag refs up front via _validate_sha_list.
    resolved_or_error = _validate_sha_list(shas, repo_dir)
    if isinstance(resolved_or_error, str):
        return resolved_or_error
    resolved = resolved_or_error

    # Check clean working tree (tracked files only)
    if (_g(["diff", "--cached", "--name-only"], repo_dir).stdout.strip()
            or _g(["diff", "--name-only"], repo_dir).stdout.strip()):
        return (
            "⚠️ CHERRY_PICK_ERROR: Working tree has staged or unstaged changes.\n"
            "Commit or restore to HEAD before cherry-picking."
        )

    # Build env with explicit committer identity for deterministic attribution
    committer_env = _ouroboros_committer_env(repo_dir)

    lock = _acquire_git_lock(ctx)
    applied: List[str] = []
    skipped: List[str] = []
    attribution_lines: List[str] = []
    try:
        for sha in resolved:
            # Capture original author date BEFORE cherry-pick (we need it for
            # --date= on the amend step; it's also what the default path
            # preserves implicitly through git's cherry-pick mechanics).
            orig_date = ""
            if override_author is not None:
                date_r = _g(["log", "-1", "--format=%aI", sha], repo_dir)
                if date_r.returncode != 0 or not date_r.stdout.strip():
                    # Defense-in-depth: the ^{commit} prevalidation makes this
                    # branch extremely unlikely, but if it ever fires mid-batch
                    # we must invalidate advisory for any earlier SHAs already
                    # applied. Otherwise repo history changes silently while
                    # advisory freshness can remain valid.
                    if applied:
                        _invalidate_advisory(
                            ctx, changed_paths=[], mutation_root=repo_dir,
                            source_tool="cherry_pick_pr_commits",
                        )
                    return (
                        f"⚠️ CHERRY_PICK_ERROR: Cannot read author date for {sha[:12]} "
                        f"(git log returned {date_r.returncode}). Aborting before "
                        f"cherry-pick to avoid losing the original timestamp.\n"
                        f"Applied before date-read failure: "
                        f"{[s for s in applied] or 'none'}"
                    )
                orig_date = date_r.stdout.strip()

            # --no-edit: replay commit as-is, keep original author + date.
            # committer_env sets GIT_COMMITTER_* explicitly for deterministic identity.
            result = _g(["cherry-pick", "--no-edit", sha], repo_dir, env=committer_env)
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "").strip()
                _g(["cherry-pick", "--abort"], repo_dir)
                if stop_on_conflict:
                    # Invalidate advisory before returning — some commits may have
                    # been successfully applied, changing repo history.
                    if applied:
                        _invalidate_advisory(
                            ctx, changed_paths=[], mutation_root=repo_dir,
                            source_tool="cherry_pick_pr_commits",
                        )
                    return (
                        f"⚠️ CHERRY_PICK_CONFLICT on {sha[:12]}:\n{err[:500]}\n\n"
                        f"Applied before conflict: {[s[:12] for s in applied] or 'none'}\n"
                        f"Run `git cherry-pick --abort` if needed, then resolve manually."
                    )
                skipped.append(sha[:12])
                continue

            # Cherry-pick succeeded — the new commit is now HEAD of the branch.
            applied.append(sha[:12])

            # If override_author is set, rewrite author via commit --amend.
            # Helpers keep this function under the 250-line gate. --date on
            # amend preserves the captured original timestamp. Committer
            # identity stays as repo-local configured (with Ouroboros
            # fallback) because committer_env sets GIT_COMMITTER_*.
            if override_author is not None:
                amend_r = _amend_author_on_head(
                    repo_dir, override_author, orig_date, committer_env,
                )
                if amend_r.returncode != 0:
                    return _rollback_failed_amend(
                        ctx, repo_dir, sha, amend_r, applied,
                    )
            # Capture ORIGINAL author from the source commit for the
            # Co-authored-by hint on the final merge commit, regardless of
            # whether author was overridden on amend. In override mode this
            # ensures the real upstream contributor still gets mentioned in
            # the merge-commit message even though their identity was rewritten
            # on the commit itself. In default mode this is the normal path.
            author_r = _g(["log", "-1", "--format=%an <%ae>", sha], repo_dir)
            if author_r.returncode != 0:
                continue
            attr = author_r.stdout.strip()
            if attr and attr not in attribution_lines:
                attribution_lines.append(attr)
    finally:
        _release_git_lock(lock)

    if not applied:
        return (
            f"⚠️ CHERRY_PICK_ERROR: No commits were successfully applied.\n"
            f"Skipped (conflict): {skipped}"
        )

    _invalidate_advisory(ctx, changed_paths=[], mutation_root=repo_dir,
                         source_tool="cherry_pick_pr_commits")

    override_note = ""
    if override_author is not None:
        override_note = (
            f"\n\nAuthor override applied: "
            f"{override_author['name']} <{override_author['email']}> "
            f"— original author dates preserved, repo-local committer "
            f"identity (Ouroboros fallback) unchanged."
        )
        attribution_description = (
            "author identity rewritten via override; "
            "original author dates and repo-local committer identity preserved"
        )
        hint_lead = (
            "Attribution: override author now appears on all cherry-picked "
            "commits in the integration branch."
        )
    else:
        attribution_description = "real commits, original authorship preserved"
        hint_lead = (
            "Attribution: original author commits preserved in integration branch."
        )

    partial = ""
    if skipped:
        partial = (
            f"\n\n⚠️ PARTIAL INGESTION — skipped (conflict): {skipped}\n"
            f"Resolve manually or re-run with those SHAs omitted."
        )

    # Rebuild author_hint with the path-appropriate lead sentence. The hint is
    # constructed inside the loop from attribution_lines; we just need to swap
    # the lead paragraph based on override_author.
    if attribution_lines:
        co_lines = "\n".join(f"Co-authored-by: {a}" for a in attribution_lines)
        author_hint = (
            f"\n\n{hint_lead}\n"
            f"Include in your final repo_commit (merge) message:\n{co_lines}"
        )
    else:
        author_hint = ""

    return (
        f"✅ Cherry-picked {len(applied)} of {len(resolved)} commit(s) onto "
        f"'{current_branch}' ({attribution_description}):\n"
        + "\n".join(f"  {sha}" for sha in applied)
        + f"\n\nNext:\n"
          f"  stage_adaptations()                      ← optional: stage Ouroboros\n"
          f"                                              adaptation changes (no commit)\n"
          f"  stage_pr_merge(branch='{current_branch}') → advisory_pre_review → repo_commit\n"
          f"  (staged adaptations land in the merge commit — no intermediate commit needed)"
        + override_note
        + author_hint
        + partial
    )


def _stage_adaptations(ctx: ToolContext) -> str:
    """Stage all current working-tree changes WITHOUT committing.

    Use this to prepare Ouroboros adaptation / fixup work that follows
    externally-authored cherry-picked commits.  Changes are staged via
    `git add -A` only — NO git commit is created here.

    IMPORTANT — correct usage sequence:
      Call stage_pr_merge DIRECTLY after stage_adaptations — do NOT run
      advisory_pre_review + repo_commit between them.  repo_commit always
      checks out ctx.branch_dev (ouroboros) first, which drops off the
      integration branch and discards the staged state.

        stage_adaptations()                     ← stage adaptation edits
        stage_pr_merge(branch='integrate/pr-N') ← merge + include staged edits
        advisory_pre_review + repo_commit       ← single merge commit on ouroboros

      The staged adaptation changes survive the stage_pr_merge checkout because
      stage_pr_merge operates on ouroboros (clean tree); the staged index
      carries over.  Adaptation changes land in the final merge commit with
      Ouroboros as the commit author.

    This complies with BIBLE.md P3: every commit passes the review gate.
    Must be on an integration branch (integrate/pr-*).
    """
    repo_dir = pathlib.Path(ctx.repo_dir)

    head_r = _g(["rev-parse", "--abbrev-ref", "HEAD"], repo_dir)
    current_branch = head_r.stdout.strip() if head_r.returncode == 0 else ""
    if not current_branch.startswith(_PR_BRANCH_PREFIX):
        return (
            f"⚠️ STAGE_ADAPTATIONS_ERROR: Current branch is '{current_branch}', "
            f"not an integration branch. Only use stage_adaptations on integrate/pr-* branches."
        )

    lock = _acquire_git_lock(ctx)
    try:
        _g(["add", "-A"], repo_dir)
        staged = _g(["diff", "--cached", "--name-only"], repo_dir).stdout.strip()
        if not staged:
            return "⚠️ STAGE_ADAPTATIONS_ERROR: Nothing to stage (working tree is clean)."
    finally:
        _release_git_lock(lock)

    _invalidate_advisory(ctx, changed_paths=[], mutation_root=repo_dir,
                         source_tool="stage_adaptations")

    files = staged.splitlines()
    return (
        f"✅ Staged {len(files)} file(s) on '{current_branch}' (NOT committed):\n"
        + "\n".join(f"  {f}" for f in files[:20])
        + (f"\n  ... and {len(files)-20} more" if len(files) > 20 else "")
        + f"\n\nNext: stage_pr_merge(branch='{current_branch}') — do NOT commit here;\n"
          f"  adaptation changes land in the merge commit on ouroboros."
    )


def _stage_pr_merge(
    ctx: ToolContext,
    branch: str,
) -> str:
    """Stage a no-fast-forward merge of an integration branch WITHOUT committing.

    Uses `git merge --no-ff --no-commit`, which:
    - Sets MERGE_HEAD so the resulting commit has both parents (merge commit)
    - Stages all diff between integration branch and ouroboros
    - Does NOT create a commit — finalize via advisory_pre_review + repo_commit

    Target branch is always `ouroboros` (ctx.branch_dev).  This is required
    because repo_commit always begins with `git checkout ctx.branch_dev`; any
    other target would lose MERGE_HEAD before the commit is created.

    ATTRIBUTION:
    - The integration branch history (cherry-picked commits) retains original
      author metadata and is permanently linked as a parent of the merge commit.
    - GitHub shows contributor graphs correctly for the cherry-picked commits.
    - Include Co-authored-by in the repo_commit message for additional visibility.

    On merge conflict:
    - `git merge --no-ff --no-commit` does NOT fully set up merge state in all
      conflict cases, so `git merge --abort` may not work reliably.
    - On failure, this function uses `git reset --hard HEAD` to restore both
      index and worktree to the clean baseline (safe: dirty-tree check confirms
      HEAD is clean before merge starts).
    """
    branch = (branch or "").strip()
    if not branch:
        return "⚠️ PR_MERGE_ERROR: branch parameter is required."
    err = _validate_git_ref_arg(branch, "branch")
    if err:
        return f"⚠️ PR_MERGE_ERROR: {err}"
    # target_branch is always branch_dev (ouroboros).  repo_commit always begins
    # with `git checkout ctx.branch_dev`, so any other target would lose MERGE_HEAD.
    target_branch = ctx.branch_dev
    if branch == target_branch:
        return (
            f"⚠️ PR_MERGE_ERROR: branch and target_branch are the same ('{branch}'). "
            f"Specify an integration branch (integrate/pr-N)."
        )

    repo_dir = pathlib.Path(ctx.repo_dir)

    if not _g(["branch", "--list", branch], repo_dir).stdout.strip():
        return f"⚠️ PR_MERGE_ERROR: Branch '{branch}' does not exist."

    # Guard: caller must already be on the integration branch.
    # We snapshot staged changes from HEAD — if HEAD != branch, the snapshot
    # would capture wrong-branch state and corrupt the adaptation carry-through.
    head_r = _g(["rev-parse", "--abbrev-ref", "HEAD"], pathlib.Path(ctx.repo_dir))
    current = head_r.stdout.strip() if head_r.returncode == 0 else ""
    if current != branch:
        return (
            f"⚠️ PR_MERGE_ERROR: Must be on '{branch}' before calling stage_pr_merge "
            f"(currently on '{current}'). Checkout the integration branch first."
        )

    lock = _acquire_git_lock(ctx)
    try:
        # Reject any unclean state on the integration branch other than staged
        # adaptations from stage_adaptations(). Use git status --porcelain so
        # ALL three states are caught:
        #   - Unstaged tracked changes (column 2 non-space): lost on checkout.
        #   - Untracked files (??): survive git checkout and would be swept into
        #     the merge commit by repo_commit. Must be staged via stage_adaptations()
        #     for any intentional new file first.
        # Staged changes (column 1 non-space, column 2 space) are fine — they are
        # the intended adaptation carry-through path.
        porcelain = _g(["status", "--porcelain"], repo_dir).stdout or ""
        dirty_lines = [
            ln for ln in porcelain.splitlines()
            if len(ln) >= 2 and (ln[1] != " " or ln[:2] == "??")
        ]
        if dirty_lines:
            sample = "\n".join(dirty_lines[:10])
            return (
                f"⚠️ PR_MERGE_ERROR: Integration branch has unstaged or untracked changes.\n"
                f"Stage all intentional changes with stage_adaptations() first.\n"
                f"Unclean files:\n{sample}"
            )

        # Save staged adaptations as a binary patch before checkout+merge.
        # `git diff --cached --binary` captures index vs HEAD (the staged changes).
        # We then hard-reset so BOTH the index AND worktree are clean before
        # checkout — `git reset HEAD -- .` only un-stages; tracked-file edits
        # would remain in the worktree and trip the dirty-tree guard on checkout.
        # On re-apply we use `git apply --index` so both index AND worktree are
        # updated, keeping them consistent and avoiding a post-apply dirty worktree.
        adaptation_patch: bytes = b""
        adapt_paths: list = []
        staged_before = _g(["diff", "--cached", "--name-only"], repo_dir).stdout.strip()
        if staged_before:
            diff_r = subprocess.run(
                ["git", "diff", "--cached", "--binary"],
                cwd=repo_dir, capture_output=True,
            )
            # Fail-closed: if patch capture fails, abort before destructive reset
            # so the caller's staged changes are preserved.
            if diff_r.returncode != 0:
                return (
                    f"⚠️ PR_MERGE_ERROR: Failed to capture staged adaptation patch "
                    f"(git diff --cached --binary returned {diff_r.returncode}). "
                    f"Staged changes are preserved. Fix the repo state and retry."
                )
            adaptation_patch = diff_r.stdout
            adapt_paths = staged_before.splitlines()
            # Hard-reset: clears both index and worktree so checkout is clean.
            _g(["reset", "--hard", "HEAD"], repo_dir)

        def _restore_on_error() -> None:
            """Restore the repo to integration branch + re-apply saved adaptations."""
            _g(["checkout", branch], repo_dir)
            if adaptation_patch:
                subprocess.run(
                    ["git", "apply", "--index", "-"],
                    cwd=repo_dir, input=adaptation_patch, capture_output=True,
                )

        co = _g(["checkout", target_branch], repo_dir)
        if co.returncode != 0:
            # Still on integration branch; re-apply adaptations there.
            if adaptation_patch:
                subprocess.run(
                    ["git", "apply", "--index", "-"],
                    cwd=repo_dir, input=adaptation_patch, capture_output=True,
                )
            return (
                f"⚠️ PR_MERGE_ERROR: Cannot checkout '{target_branch}': "
                f"{_sanitize_git_error((co.stderr or '').strip())}"
            )

        # Reject unstaged tracked changes on the target branch.
        if _g(["diff", "--name-only"], repo_dir).stdout.strip():
            _restore_on_error()
            return (
                f"⚠️ PR_MERGE_ERROR: Working tree on '{target_branch}' has unstaged "
                f"tracked changes.\nCommit or restore to HEAD before merging."
            )

        merge = _g(["merge", "--no-ff", "--no-commit", branch], repo_dir)
        if merge.returncode != 0:
            err = (merge.stderr or merge.stdout or "").strip()
            # `--no-ff --no-commit` may not fully set up merge state on conflict.
            # Reset hard to clean state, then restore integration branch + adaptations.
            _g(["reset", "--hard", "HEAD"], repo_dir)
            _restore_on_error()
            return (
                f"⚠️ PR_MERGE_ERROR: Merge failed (restored to integration branch):\n"
                f"{_sanitize_git_error(err[:500])}"
            )

        # Verify MERGE_HEAD was set — if git returns 0 with "Already up to date.",
        # no merge state is established and repo_commit would create a plain commit.
        merge_head = _g(["rev-parse", "-q", "--verify", "MERGE_HEAD"], repo_dir)
        if merge_head.returncode != 0:
            _restore_on_error()
            return (
                f"⚠️ PR_MERGE_ERROR: Branch '{branch}' is already fully merged into "
                f"'{target_branch}' (nothing to merge / already up to date)."
            )

        # Re-apply saved adaptations on top of the merged state so they land
        # in the final merge commit created by advisory_pre_review + repo_commit.
        # `--index` updates both index and worktree for consistency.
        if adaptation_patch:
            apply_r = subprocess.run(
                ["git", "apply", "--index", "-"],
                cwd=repo_dir, input=adaptation_patch, capture_output=True,
            )
            if apply_r.returncode != 0:
                apply_err = (apply_r.stderr or apply_r.stdout or b"").decode(
                    "utf-8", errors="replace"
                ).strip()
                # Restore to integration branch before reporting error.
                _g(["reset", "--hard", "HEAD"], repo_dir)
                _restore_on_error()
                return (
                    f"⚠️ PR_MERGE_ERROR: Merge staged but adaptation re-apply failed "
                    f"(patch conflicts with merged tree). Restored to integration branch.\n"
                    f"Resolve manually: edit files, stage with git add, then retry.\n"
                    f"{_sanitize_git_error(apply_err[:300])}"
                )
    finally:
        _release_git_lock(lock)

    # Attribution hint from merged branch
    base_r = _g(["merge-base", target_branch, branch], repo_dir)
    authors = []
    if base_r.returncode == 0:
        log_r = _g(["log", "--format=%an <%ae>",
                    f"{base_r.stdout.strip()}..{branch}"], repo_dir)
        if log_r.returncode == 0:
            authors = sorted(set(log_r.stdout.strip().splitlines()))

    _invalidate_advisory(ctx, changed_paths=[], mutation_root=repo_dir,
                         source_tool="stage_pr_merge")

    co_authored = "\n".join(f"Co-authored-by: {a}" for a in authors) if authors else ""
    author_hint = (
        f"\n\nAttribution — include in repo_commit message:\n{co_authored}"
        if co_authored else ""
    )

    return (
        f"✅ Staged merge '{branch}' → '{target_branch}' (NOT committed)\n"
        f"  MERGE_HEAD is set — repo_commit will create a proper merge commit\n"
        f"  with both parents, preserving integration branch history.\n"
        f"  Branch '{branch}' left intact.\n\n"
        f"Next:\n"
        f"  advisory_pre_review(commit_message='...')\n"
        f"  repo_commit(commit_message='...')"
        + author_hint
    )


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("fetch_pr_ref", {
            "name": "fetch_pr_ref",
            "description": (
                "Fetch a GitHub PR's commits locally via pull/{n}/head ref. "
                "Uses force-update prefix so rebased/force-pushed PRs refetch correctly. "
                "Preserves original author metadata exactly as pushed. "
                "After fetching, list commit SHAs for cherry_pick_pr_commits."
            ),
            "parameters": {"type": "object", "properties": {
                "pr_number": {"type": "integer", "description": "GitHub PR number"},
                "remote": {"type": "string", "default": "origin",
                           "description": "Git remote name (default: origin)"},
            }, "required": ["pr_number"]},
        }, _fetch_pr_ref, is_code_tool=True),

        ToolEntry("create_integration_branch", {
            "name": "create_integration_branch",
            "description": (
                "Create a fresh integration branch (integrate/pr-N) from ouroboros. "
                "External cherry-picked commits and Ouroboros adaptation changes are "
                "kept separate here before merging."
            ),
            "parameters": {"type": "object", "properties": {
                "pr_number": {"type": "integer", "description": "GitHub PR number"},
                "base_branch": {"type": "string", "default": "ouroboros",
                                "description": "Branch to create from"},
            }, "required": ["pr_number"]},
        }, _create_integration_branch, is_code_tool=True),

        ToolEntry("cherry_pick_pr_commits", {
            "name": "cherry_pick_pr_commits",
            "description": (
                "Replay PR commits onto the current integration branch using "
                "git cherry-pick --no-edit. By default each commit is created with "
                "the original author name/email/date preserved — GitHub attribution "
                "is real, not just a Co-authored-by annotation. Committer identity "
                "is set explicitly from the repo-local git config (with Ouroboros "
                "fallback when local identity is missing) for deterministic attribution. "
                "Optional override_author={'name': 'X', 'email': 'Y'} rewrites "
                "author name+email on every cherry-picked commit via git commit "
                "--amend --author --date while preserving the original author DATE "
                "and the repo-local committer (with Ouroboros fallback). Use when external contributor's "
                "commits use placeholder identity (e.g. ran Ouroboros locally "
                "without configuring git user.email). The override applies to "
                "the entire batch uniformly. "
                "Must be on an integrate/pr-N branch. "
                "When stop_on_conflict=False, skipped SHAs are explicitly reported."
            ),
            "parameters": {"type": "object", "properties": {
                "shas": {"type": "array", "items": {"type": "string"},
                         "description": "Ordered list of commit SHAs (oldest first)"},
                "stop_on_conflict": {"type": "boolean", "default": True,
                                     "description": "Abort on first conflict (default: true)"},
                "override_author": {
                    "type": "object",
                    "description": (
                        "Optional: rewrite author name+email on all cherry-picked "
                        "commits. Original author date and repo-local committer "
                        "identity with Ouroboros fallback when local identity is "
                        "missing are preserved. Applied to the entire batch uniformly."
                    ),
                    "properties": {
                        "name": {"type": "string",
                                 "description": "Author display name (no newlines, '<', or '>')"},
                        "email": {"type": "string",
                                  "description": "Author email (must contain '@', no newlines or angle brackets)"},
                    },
                    "required": ["name", "email"],
                    "additionalProperties": False,
                },
            }, "required": ["shas"]},
        }, _cherry_pick_pr_commits, is_code_tool=True),

        ToolEntry("stage_adaptations", {
            "name": "stage_adaptations",
            "description": (
                "Stage all current working-tree changes on the integration branch WITHOUT "
                "committing (git add -A only). Use after cherry_pick_pr_commits to prepare "
                "Ouroboros adaptation/fixup changes. Finalize via advisory_pre_review + "
                "repo_commit to comply with BIBLE.md P3 (all commits must pass review). "
                "Must be on an integrate/pr-N branch."
            ),
            "parameters": {"type": "object", "properties": {}},
        }, _stage_adaptations, is_code_tool=True),

        ToolEntry("stage_pr_merge", {
            "name": "stage_pr_merge",
            "description": (
                "Stage a no-fast-forward merge of an integration branch into ouroboros "
                "WITHOUT committing (git merge --no-ff --no-commit). Sets MERGE_HEAD so "
                "repo_commit creates a proper merge commit with both parents. Target is "
                "always ouroboros (repo_commit always checks out branch_dev before "
                "committing — any other target would lose MERGE_HEAD). The "
                "integration-branch history (with original author commits) is permanently "
                "linked. Finalize via advisory_pre_review + repo_commit."
            ),
            "parameters": {"type": "object", "properties": {
                "branch": {"type": "string",
                           "description": "Integration branch to merge (e.g. integrate/pr-17)"},
            }, "required": ["branch"]},
        }, _stage_pr_merge, is_code_tool=True),
    ]
