"""Worker-boot checks for dirty repo, version sync, budget, and memory files."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Any, Dict, Tuple

from ouroboros.utils import utc_now_iso, read_text, append_jsonl, read_json_dict

log = logging.getLogger(__name__)


def _is_release_tag(tag: str) -> bool:
    from ouroboros.tools.release_sync import normalize_release_tag

    return bool(normalize_release_tag(tag))


def check_uncommitted_changes(env: Any) -> Tuple[dict, int]:
    """Warn on dirty worker boot; rescue/reset is supervisor-owned, never worker-owned."""
    try:
        lock_path = env.repo_path(".git/index.lock")
        if lock_path.exists():
            try:
                lock_path.unlink()
                log.warning("Removed stale git index.lock")
            except OSError:
                pass

        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(env.repo_dir),
            capture_output=True, text=True, timeout=10, check=True
        )
        dirty_files = [l.strip() for l in result.stdout.strip().split('\n') if l.strip()]
        if dirty_files:
            log.warning(
                "Uncommitted changes detected on worker boot; diagnostic-only, "
                "rescue is owned by supervisor-side safe_restart(rescue_and_reset)"
            )
            return {
                "status": "warning",
                "files": dirty_files[:20],
                "auto_committed": False,
                "auto_rescue_skipped": "supervisor_side_rescue_owns_this",
            }, 1
        return {"status": "ok"}, 0
    except Exception as e:
        return {"status": "error", "error": str(e)}, 0


def check_version_sync(env: Any) -> Tuple[dict, int]:
    """Check VERSION file sync with git tags and pyproject.toml."""
    try:
        from ouroboros.tools.release_sync import (
            _normalize_pep440,
            _shields_escape,
            extract_architecture_header_version,
            extract_readme_badge_version,
            is_release_version,
        )
        version_file = read_text(env.repo_path("VERSION")).strip()
        issue_count = 0
        result_data: Dict[str, Any] = {"version_file": version_file}

        pyproject_path = env.repo_path("pyproject.toml")
        pyproject_content = read_text(pyproject_path)
        match = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', pyproject_content, re.MULTILINE)
        if match:
            pyproject_version = match.group(1)
            result_data["pyproject_version"] = pyproject_version
            expected_pyproject = _normalize_pep440(version_file) if is_release_version(version_file) else version_file
            if expected_pyproject != pyproject_version:
                result_data["status"] = "warning"
                issue_count += 1

        try:
            readme_content = read_text(env.repo_path("README.md"))
            badge_version = extract_readme_badge_version(readme_content)
            readme_version = badge_version
            if not readme_version:
                readme_match = re.search(r'\*\*Version:\*\*\s*([^\s]+)', readme_content)
                readme_version = str(readme_match.group(1) or "").strip() if readme_match else ""
            if readme_version:
                result_data["readme_version"] = readme_version
                badge_token_ok = True
                if badge_version and is_release_version(version_file):
                    badge_token_ok = f"version-{_shields_escape(version_file)}-green" in readme_content
                result_data["readme_badge_url_valid"] = badge_token_ok
                if version_file != readme_version or not badge_token_ok:
                    result_data["status"] = "warning"
                    issue_count += 1
        except Exception:
            log.debug("Failed to check README.md version", exc_info=True)

        try:
            arch_content = read_text(env.repo_path("docs/ARCHITECTURE.md"))
            arch_version = extract_architecture_header_version(arch_content)
            if arch_version:
                result_data["architecture_version"] = arch_version
                if version_file != arch_version:
                    result_data["status"] = "warning"
                    issue_count += 1
        except Exception:
            log.debug("Failed to check ARCHITECTURE.md version", exc_info=True)

        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=str(env.repo_dir),
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            result_data["status"] = "warning"
            result_data["message"] = "no_tags"
            return result_data, issue_count
        else:
            latest_tag = result.stdout.strip().lstrip('v')
            result_data["latest_tag"] = latest_tag
            if _is_release_tag(latest_tag) and version_file != latest_tag:
                result_data["status"] = "warning"
                issue_count += 1
            elif not _is_release_tag(latest_tag):
                result_data["tag_sync"] = "ignored_non_release_tag"

        if issue_count == 0:
            result_data["status"] = "ok"

        return result_data, issue_count
    except Exception as e:
        return {"status": "error", "error": str(e)}, 0


def check_budget(env: Any) -> Tuple[dict, int]:
    """Check budget remaining with warning thresholds."""
    try:
        state_path = env.drive_path("state") / "state.json"
        state_data = read_json_dict(state_path)
        if state_data is None:
            return {
                "status": "error",
                "error": "state.json missing or invalid",
                "path": str(state_path),
            }, 1
        total_budget_str = os.environ.get("TOTAL_BUDGET", "")

        if not total_budget_str or float(total_budget_str) == 0:
            return {"status": "unconfigured"}, 0
        else:
            total_budget = float(total_budget_str)
            spent = float(state_data.get("spent_usd", 0))
            remaining = max(0, total_budget - spent)

            if remaining < 0.5:
                status = "emergency"
                issues = 1
            elif remaining < 2:
                status = "critical"
                issues = 1
            elif remaining < 5:
                status = "warning"
                issues = 0
            else:
                status = "ok"
                issues = 0

            return {
                "status": status,
                "remaining_usd": round(remaining, 2),
                "total_usd": total_budget,
                "spent_usd": round(spent, 2),
            }, issues
    except Exception as e:
        return {"status": "error", "error": str(e)}, 0


def check_review_continuations(env: Any) -> Tuple[dict, int]:
    try:
        from ouroboros.task_continuation import list_review_continuations
        from ouroboros.task_results import (
            STATUS_CANCELLED,
            STATUS_COMPLETED,
            STATUS_FAILED,
            STATUS_INTERRUPTED,
            STATUS_REJECTED_DUPLICATE,
            STATUS_REQUESTED,
            STATUS_RUNNING,
            STATUS_SCHEDULED,
            list_task_results,
        )

        continuations, corrupt = list_review_continuations(env.drive_root)
        task_rows = list_task_results(
            env.drive_root,
            statuses=[
                STATUS_REQUESTED,
                STATUS_SCHEDULED,
                STATUS_RUNNING,
                STATUS_INTERRUPTED,
                STATUS_COMPLETED,
                STATUS_FAILED,
                STATUS_CANCELLED,
                STATUS_REJECTED_DUPLICATE,
            ],
        )
        task_by_id = {
            str(item.get("task_id") or ""): item
            for item in task_rows
            if str(item.get("task_id") or "").strip()
        }

        rows = []
        interrupted = []
        for item in continuations:
            task_status = str((task_by_id.get(item.task_id) or {}).get("status") or "")
            row = {
                "task_id": item.task_id,
                "task_status": task_status or "missing",
                "source": item.source,
                "stage": item.stage,
                "repo_key": item.repo_key,
                "tool_name": item.tool_name,
                "attempt": item.attempt,
                "block_reason": item.block_reason,
                "obligation_ids": list(item.obligation_ids or []),
                "critical_findings": len(item.critical_findings or []),
                "advisory_findings": len(item.advisory_findings or []),
                "updated_ts": item.updated_ts,
            }
            rows.append(row)
            if task_status == STATUS_INTERRUPTED:
                interrupted.append(row)

        status = "ok"
        issues = 0
        if rows or corrupt:
            status = "warning"
        if rows:
            issues += 1
        if corrupt:
            status = "error"
            issues += 1

        return {
            "status": status,
            "open_review_continuations": rows[:20],
            "interrupted_tasks": interrupted[:20],
            "corrupt": corrupt[:20],
        }, issues
    except Exception as e:
        return {"status": "error", "error": str(e)}, 1


def verify_system_state(env: Any, git_sha: str) -> None:
    """Bible Principle 1: verify system state on every startup."""
    checks: Dict[str, Any] = {}
    issues = 0
    drive_logs = env.drive_path("logs")

    checks["uncommitted_changes"], issue_count = check_uncommitted_changes(env)
    issues += issue_count

    checks["version_sync"], issue_count = check_version_sync(env)
    issues += issue_count

    checks["budget"], issue_count = check_budget(env)
    issues += issue_count

    memory_dir = env.drive_path("memory")
    identity_path = memory_dir / "identity.md"
    scratchpad_path = memory_dir / "scratchpad.md"
    world_path = memory_dir / "WORLD.md"

    identity_ok = identity_path.exists() and identity_path.stat().st_size > 0
    scratchpad_ok = scratchpad_path.exists()
    world_ok = world_path.exists()

    checks["identity"] = {"exists": identity_path.exists(), "non_empty": identity_ok}
    checks["scratchpad"] = {"exists": scratchpad_ok}
    checks["world_profile"] = {"exists": world_ok}

    if not identity_ok:
        issues += 1
        log.warning("identity.md missing or empty — continuity at risk (Bible P1)")
    if not scratchpad_ok:
        issues += 1
        log.warning("scratchpad.md missing — working memory not available (Bible P1)")
    if not world_ok:
        issues += 1
        log.warning("WORLD.md missing — environment profile not available")

    configured_model = os.environ.get("OUROBOROS_MODEL", "")
    checks["model"] = {"configured": configured_model or "(not set)"}
    if not configured_model:
        issues += 1

    # Reconcile stale hung reviewed attempts left by abrupt process death
    try:
        import pathlib
        from ouroboros.review_state import _utc_now, update_state
        drive_root = pathlib.Path(env.drive_root) if hasattr(env, "drive_root") else env.drive_path("").parent
        expired = update_state(
            drive_root,
            lambda st: st.expire_stale_attempts(now_ts=_utc_now()),
        )
        if expired:
            log.warning("Auto-expired %d stale reviewed attempt(s) on startup", len(expired))
    except Exception:
        log.debug("Failed to reconcile commit attempt state", exc_info=True)

    checks["review_continuations"], issue_count = check_review_continuations(env)
    issues += issue_count

    event = {
        "ts": utc_now_iso(),
        "type": "startup_verification",
        "checks": checks,
        "issues_count": issues,
        "git_sha": git_sha,
    }
    append_jsonl(drive_logs / "events.jsonl", event)

    if issues > 0:
        log.warning(f"Startup verification found {issues} issue(s): {checks}")


def inject_crash_report(env: Any) -> None:
    """If a crash report exists from a rollback, log it to events.

    The file is NOT deleted — it stays so that build_health_invariants()
    shows CRITICAL: RECENT CRASH ROLLBACK on every task until the issue
    is investigated and removed via run_command (LLM-first, P5).
    """
    try:
        crash_path = env.drive_path("state") / "crash_report.json"
        if not crash_path.exists():
            return
        crash_data = read_json_dict(crash_path)
        if not crash_data:
            append_jsonl(env.drive_path("logs") / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "crash_report_invalid",
                "path": str(crash_path),
            })
            log.warning("Crash report exists but is not valid JSON object: %s", crash_path)
            return
        append_jsonl(env.drive_path("logs") / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": "crash_rollback_detected",
            "crash_data": crash_data,
        })
        log.warning("Crash rollback detected: %s", crash_data)
    except Exception:
        log.debug("Failed to process crash report", exc_info=True)


def verify_restart(env: Any, git_sha: str) -> None:
    """Best-effort restart verification."""
    try:
        pending_path = env.drive_path('state') / 'pending_restart_verify.json'
        claim_path = pending_path.with_name(f"pending_restart_verify.claimed.{os.getpid()}.json")
        try:
            os.rename(str(pending_path), str(claim_path))
        except (FileNotFoundError, Exception):
            return
        try:
            claim_data = read_json_dict(claim_path)
            if claim_data is None:
                append_jsonl(env.drive_path('logs') / 'events.jsonl', {
                    'ts': utc_now_iso(), 'type': 'restart_verify',
                    'pid': os.getpid(), 'ok': False,
                    'error': 'pending_restart_verify_invalid',
                    'observed_sha': git_sha,
                })
                return
            expected_sha = str(claim_data.get("expected_sha", "")).strip()
            ok = bool(expected_sha and expected_sha == git_sha)
            append_jsonl(env.drive_path('logs') / 'events.jsonl', {
                'ts': utc_now_iso(), 'type': 'restart_verify',
                'pid': os.getpid(), 'ok': ok,
                'expected_sha': expected_sha, 'observed_sha': git_sha,
            })
        except Exception:
            log.debug("Failed to log restart verify event", exc_info=True)
            pass
        try:
            claim_path.unlink()
        except Exception:
            log.debug("Failed to delete restart verify claim file", exc_info=True)
            pass
    except Exception:
        log.debug("Restart verification failed", exc_info=True)
        pass
