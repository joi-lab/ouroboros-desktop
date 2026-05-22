from __future__ import annotations

import contextlib
import logging
import os
import pathlib
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict

from ouroboros.config import get_skills_repo_path, load_settings
from ouroboros.skill_lifecycle_queue import (
    DuplicateLifecycleJobError,
    JobProgressTarget,
    LifecycleJob,
    LifecycleJobOptions,
    run_blocking_preserving_cancellation,
    run_lifecycle_job,
    run_lifecycle_job_blocking,
)
from ouroboros.skill_loader import (
    SkillPayloadUnreadable,
    SkillReviewState,
    compute_content_hash,
    find_skill,
    requested_core_setting_keys,
    review_status_allows_execution,
    save_enabled,
    save_review_state,
    save_skill_grants,
    skill_review_gate,
    skill_state_dir,
)
from ouroboros.skill_review_status import (
    STATUS_BLOCKERS,
    STATUS_PENDING,
    STATUS_WARNINGS,
    normalize_skill_review_status,
)
from ouroboros.skill_review import SkillReviewOutcome, review_skill as _default_review_skill
from ouroboros.utils import append_jsonl, atomic_write_json, read_json_dict, utc_now_iso

log = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL_SEC = 30.0
_STALE_REVIEW_JOB_SEC = int(os.environ.get("OUROBOROS_SKILL_REVIEW_JOB_STALE_SEC", "7200"))


ReviewImpl = Callable[[Any, str], SkillReviewOutcome]


def review_job_state_path(drive_root: pathlib.Path, skill_name: str) -> pathlib.Path:
    return skill_state_dir(pathlib.Path(drive_root), skill_name) / "review_job.json"


def _events_path(drive_root: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(drive_root) / "logs" / "events.jsonl"


def _chat_jsonl_path(drive_root: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(drive_root) / "logs" / "chat.jsonl"


def _progress_jsonl_path(drive_root: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(drive_root) / "logs" / "progress.jsonl"


def _read_review_job(path: pathlib.Path) -> Dict[str, Any]:
    return read_json_dict(path) or {}


def _pid_alive(pid: int) -> bool:
    from ouroboros.platform_layer import pid_is_alive

    return pid_is_alive(pid)


def _iso_age_sec(value: str) -> float:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return 0.0


def _review_lifecycle_chat_task_id(skill_name: str, job_id: str) -> str:
    skill_suffix = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(skill_name or "skill")).strip("_")
    job_suffix = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(job_id or "")).strip("_")
    return f"skill_lifecycle_review_{skill_suffix or 'skill'}_{job_suffix or 'job'}"


def _append_interrupted_review_progress(
    drive_root: pathlib.Path,
    skill_name: str,
    payload: Dict[str, Any],
    *,
    ts: str,
) -> None:
    reason = str(payload.get("interrupt_reason") or "interrupted")
    job_id = str(payload.get("job_id") or "")
    lifecycle = {
        "id": job_id,
        "kind": "review",
        "target": skill_name,
        "status": "interrupted",
        "phase": "interrupted",
        "message": "Review job was interrupted before completion.",
        "error": reason,
        "stale": False,
        "stale_reason": reason,
        "recovery_hint": "Start a fresh review for this skill before enabling or granting access.",
    }
    text = f"Skill review: `{skill_name}` — interrupted — {reason}"
    append_jsonl(
        _progress_jsonl_path(drive_root),
        {
            "ts": ts,
            "type": "send_message",
            "task_id": _review_lifecycle_chat_task_id(skill_name, job_id),
            "is_progress": True,
            "direction": "out",
            "chat_id": 0,
            "user_id": 0,
            "text": text,
            "content": text,
            "format": "",
            "lifecycle": lifecycle,
        },
    )


def mark_stale_review_job_interrupted(
    drive_root: pathlib.Path,
    skill_name: str,
    *,
    current_content_hash: str = "",
    stale_after_sec: int = _STALE_REVIEW_JOB_SEC,
) -> None:
    path = review_job_state_path(drive_root, skill_name)
    data = _read_review_job(path)
    if str(data.get("status") or "") != "running":
        return
    pid = int(data.get("pid") or 0)
    heartbeat_age = _iso_age_sec(str(data.get("last_heartbeat_at") or data.get("started_at") or ""))
    pid_dead = bool(pid and not _pid_alive(pid))
    heartbeat_stale = bool(heartbeat_age and heartbeat_age > stale_after_sec)
    if not (pid_dead or heartbeat_stale):
        return
    now = utc_now_iso()
    payload = {
        **data,
        "status": "interrupted",
        "finished_at": now,
        "interrupted_at": now,
        "interrupt_reason": "owner_process_exited" if pid_dead else "heartbeat_stale",
        "content_hash": data.get("content_hash") or current_content_hash,
    }
    atomic_write_json(path, payload, trailing_newline=True)
    _append_interrupted_review_progress(drive_root, skill_name, payload, ts=now)
    append_jsonl(
        _events_path(drive_root),
        {
            "ts": now,
            "type": "skill_review_interrupted",
            "skill": skill_name,
            "content_hash": payload.get("content_hash", ""),
            "job_id": payload.get("job_id", ""),
            "reason": payload.get("interrupt_reason", ""),
        },
    )


def reconcile_stale_review_jobs(
    drive_root: pathlib.Path,
    *,
    stale_after_sec: int = _STALE_REVIEW_JOB_SEC,
) -> int:
    root = pathlib.Path(drive_root) / "state" / "skills"
    if not root.exists():
        return 0
    count = 0
    for path in root.glob("*/review_job.json"):
        before = _read_review_job(path)
        if str(before.get("status") or "") != "running":
            continue
        skill_name = path.parent.name
        mark_stale_review_job_interrupted(
            pathlib.Path(drive_root),
            skill_name,
            current_content_hash=str(before.get("content_hash") or ""),
            stale_after_sec=stale_after_sec,
        )
        after = _read_review_job(path)
        if str(after.get("status") or "") == "interrupted":
            count += 1
    return count


def _patch_review_job(
    drive_root: pathlib.Path,
    skill_name: str,
    *,
    expected_job_id: str = "",
    **updates: Any,
) -> None:
    path = review_job_state_path(drive_root, skill_name)
    data = _read_review_job(path)
    current_job_id = str(data.get("job_id") or "")
    if expected_job_id and current_job_id and current_job_id != expected_job_id:
        return
    data.update(updates)
    atomic_write_json(path, data, trailing_newline=True)


@contextlib.contextmanager
def _review_job_heartbeat(drive_root: pathlib.Path, skill_name: str):
    stop = threading.Event()
    expected_job_id = str(
        _read_review_job(review_job_state_path(drive_root, skill_name)).get("job_id") or ""
    )

    def _beat() -> None:
        while not stop.wait(_HEARTBEAT_INTERVAL_SEC):
            try:
                _patch_review_job(
                    drive_root,
                    skill_name,
                    expected_job_id=expected_job_id,
                    last_heartbeat_at=utc_now_iso(),
                )
            except Exception:
                log.debug("skill review heartbeat update failed", exc_info=True)

    thread = threading.Thread(target=_beat, name=f"skill-review-heartbeat-{skill_name}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1.0)


def _skill_content_hash(drive_root: pathlib.Path, skill_name: str, repo_path: str | None) -> str:
    skill = find_skill(drive_root, skill_name, repo_path=repo_path)
    if skill is None or skill.load_error:
        return ""
    try:
        return compute_content_hash(
            skill.skill_dir,
            manifest_entry=skill.manifest.entry,
            manifest_scripts=skill.manifest.scripts,
        )
    except SkillPayloadUnreadable:
        return ""


def _review_dedupe_key(skill_name: str, content_hash: str) -> str:
    suffix = content_hash or "unknown"
    return f"review:{skill_name}:{suffix}"


def _call_review_with_lifecycle_guard(
    review_impl: ReviewImpl,
    ctx: Any,
    skill_name: str,
) -> SkillReviewOutcome:
    sentinel = object()
    previous = {
        "_skill_review_lifecycle_guard": getattr(ctx, "_skill_review_lifecycle_guard", sentinel),
        "_skill_review_lifecycle_job_id": getattr(ctx, "_skill_review_lifecycle_job_id", sentinel),
    }
    job_data = _read_review_job(review_job_state_path(pathlib.Path(ctx.drive_root), skill_name))
    setattr(ctx, "_skill_review_lifecycle_guard", True)
    setattr(ctx, "_skill_review_lifecycle_job_id", str(job_data.get("job_id") or ""))
    try:
        return review_impl(ctx, skill_name)
    finally:
        for attr, value in previous.items():
            if value is sentinel:
                with contextlib.suppress(AttributeError):
                    delattr(ctx, attr)
            else:
                setattr(ctx, attr, value)


async def _to_thread_preserving_result(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await run_blocking_preserving_cancellation(
        func,
        *args,
        log_label="blocking skill review lifecycle work",
        **kwargs,
    )


def _emit_review_persist_skipped(
    drive_root: pathlib.Path,
    skill_name: str,
    content_hash: str,
    *,
    reason: str,
    job_status: str = "",
    job_id: str = "",
) -> None:
    now = utc_now_iso()
    append_jsonl(
        _events_path(drive_root),
        {
            "ts": now,
            "type": "skill_review_persist_skipped",
            "skill": skill_name,
            "content_hash": content_hash,
            "reason": reason,
            "job_status": job_status,
            "job_id": job_id,
        },
    )


def _can_persist_review_outcome(
    drive_root: pathlib.Path,
    skill_name: str,
    content_hash: str,
    *,
    expected_job_id: str = "",
) -> bool:
    data = _read_review_job(review_job_state_path(pathlib.Path(drive_root), skill_name))
    if not data:
        return True
    status = str(data.get("status") or "")
    job_hash = str(data.get("content_hash") or "")
    job_id = str(data.get("job_id") or "")

    def _skip(reason: str) -> bool:
        _emit_review_persist_skipped(
            pathlib.Path(drive_root),
            skill_name,
            content_hash,
            reason=reason,
            job_status=status,
            job_id=job_id,
        )
        return False

    if expected_job_id and job_id and job_id != expected_job_id:
        return _skip("review job id no longer matches lifecycle owner")
    if job_hash and job_hash != content_hash:
        return _skip("content hash no longer matches current review job")
    terminal_blocking = {"interrupted", "failed", "cancelled"}
    if status in terminal_blocking and (not job_hash or job_hash == content_hash):
        return _skip(f"review job already {status}")
    return True


def _review_job_finish_skip_reason(
    drive_root: pathlib.Path,
    skill_name: str,
    job_id: str,
) -> str:
    if not job_id:
        return ""
    data = _read_review_job(review_job_state_path(pathlib.Path(drive_root), skill_name))
    current_job_id = str(data.get("job_id") or "")
    if current_job_id and current_job_id != job_id:
        return "review job id no longer matches lifecycle owner"
    status = str(data.get("status") or "")
    if status and status != "running":
        return f"review job already {status}"
    return ""


def _reconcile_deps_after_pass_review(
    drive_root: pathlib.Path,
    skill_name: str,
    *,
    repo_path: str | None = None,
) -> tuple[str, str]:
    try:
        from ouroboros.marketplace.install_specs import install_specs_hash
        from ouroboros.marketplace.isolated_deps import (
            install_isolated_dependencies,
            read_deps_state,
        )

        loaded = find_skill(drive_root, skill_name, repo_path=repo_path)
        if loaded is None:
            return "failed", "skill not found during dependency reconciliation"
        from ouroboros.skill_dependencies import auto_install_specs_for_skill

        auto_specs = auto_install_specs_for_skill(drive_root, loaded)
        if not auto_specs:
            return "not_required", ""
        deps_state = read_deps_state(drive_root, skill_name, loaded.skill_dir)
        expected_hash = install_specs_hash(auto_specs)
        if (
            str(deps_state.get("status") or "") == "installed"
            and deps_state.get("specs_hash") == expected_hash
        ):
            return "installed", ""
        install_isolated_dependencies(drive_root, skill_name, loaded.skill_dir, auto_specs)
        return "installed", ""
    except Exception as exc:
        log.debug("post-review deps reconcile failed", exc_info=True)
        return "failed", f"{type(exc).__name__}: {exc}"


def _heal_mode(ctx: Any) -> bool:
    try:
        constraint = getattr(ctx, "task_constraint", None)
        return bool(constraint and getattr(constraint, "mode", "") == "skill_repair")
    except Exception:
        return False


def _outcome_payload(
    outcome: SkillReviewOutcome,
    *,
    deps_status: str,
    deps_error: str,
    extension_action: Any,
    extension_reason: Any,
    job: LifecycleJob | None = None,
) -> Dict[str, Any]:
    status = normalize_skill_review_status(outcome.status)
    gate = skill_review_gate(status)
    payload: Dict[str, Any] = {
        "skill": outcome.skill_name,
        "status": status,
        "content_hash": outcome.content_hash,
        "reviewer_models": outcome.reviewer_models,
        "findings": outcome.findings,
        "raw_actor_records": list(getattr(outcome, "raw_actor_records", []) or []),
        "advisory_result": dict(getattr(outcome, "advisory_result", {}) or {}),
        "error": outcome.error,
        "review_gate": gate,
        "executable_review": gate["executable_review"],
        "auto_flow": bool(getattr(outcome, "auto_flow", False)),
        "auto_granted_keys": list(getattr(outcome, "auto_granted_keys", []) or []),
        "requested_keys": list(getattr(outcome, "requested_keys", []) or []),
        "auto_granted_permissions": list(getattr(outcome, "auto_granted_permissions", []) or []),
        "requested_permissions": list(getattr(outcome, "requested_permissions", []) or []),
        "deps_status": deps_status,
        "deps_error": deps_error,
        "extension_action": extension_action,
        "extension_reason": extension_reason,
    }
    if getattr(outcome, "convergence_hint", ""):
        payload["convergence_hint"] = outcome.convergence_hint
    if job is not None:
        payload["job_id"] = job.id
        payload["job_status"] = job.status
    return payload


def _reconcile_extension_payload(
    ctx: Any,
    skill_name: str,
    *,
    repo_path: str | None,
    heal_mode: bool,
) -> tuple[Any, Any]:
    if heal_mode:
        try:
            from ouroboros import extension_loader

            if skill_name in extension_loader.snapshot()["extensions"]:
                extension_loader.unload_extension(skill_name)
                return "extension_unloaded", "heal_review_only"
            return "extension_heal_review_only", "heal_review_only"
        except Exception:
            return "extension_heal_review_only", "heal_review_only"
    try:
        from ouroboros import extension_loader

        live_state = extension_loader.reconcile_extension(
            skill_name,
            pathlib.Path(ctx.drive_root),
            load_settings,
            repo_path=repo_path,
            retry_load_error=True,
        )
        return live_state.get("action"), live_state.get("reason")
    except Exception:
        return None, None


def _on_started(
    drive_root: pathlib.Path,
    skill_name: str,
    content_hash: str,
    started_monotonic: Dict[str, float],
) -> Callable[[LifecycleJob], None]:
    def _callback(job: LifecycleJob) -> None:
        now = utc_now_iso()
        started_monotonic["value"] = time.monotonic()
        payload = {
            "status": "running",
            "skill": skill_name,
            "content_hash": content_hash,
            "job_id": job.id,
            "lifecycle_status": job.status,
            "dedupe_key": job.dedupe_key,
            "started_at": job.started_at or now,
            "last_heartbeat_at": now,
            "finished_at": "",
            "duration_sec": None,
            "pid": os.getpid(),
        }
        atomic_write_json(review_job_state_path(drive_root, skill_name), payload, trailing_newline=True)
        append_jsonl(
            _events_path(drive_root),
            {
                "ts": now,
                "type": "skill_review_started",
                "skill": skill_name,
                "content_hash": content_hash,
                "job_id": job.id,
            },
        )

    return _callback


def _on_finished(
    drive_root: pathlib.Path,
    skill_name: str,
    content_hash: str,
    started_monotonic: Dict[str, float],
) -> Callable[[LifecycleJob, Any, BaseException | None], None]:
    def _callback(job: LifecycleJob, result: Any, exc: BaseException | None) -> None:
        now = utc_now_iso()
        duration = None
        if "value" in started_monotonic:
            duration = round(max(0.0, time.monotonic() - started_monotonic["value"]), 3)
        skip_reason = ""
        if "value" not in started_monotonic:
            skip_reason = "review job never acquired lifecycle file lock"
        else:
            skip_reason = _review_job_finish_skip_reason(drive_root, skill_name, job.id)
        if skip_reason:
            append_jsonl(
                _events_path(drive_root),
                {
                    "ts": now,
                    "type": "skill_review_finish_skipped",
                    "skill": skill_name,
                    "content_hash": content_hash,
                    "job_id": job.id,
                    "reason": skip_reason,
                    "duration_sec": duration,
                },
            )
            return
        review_status = normalize_skill_review_status(getattr(result, "status", "") if result is not None else "")
        error = str(exc) if exc is not None else (getattr(result, "error", "") if result is not None else "")
        deps_error = getattr(result, "deps_error", "") if result is not None else ""
        state_status = "failed" if job.status in {"failed", "cancelled"} else "completed"
        payload = {
            "status": state_status,
            "skill": skill_name,
            "content_hash": getattr(result, "content_hash", "") or content_hash,
            "job_id": job.id,
            "lifecycle_status": job.status,
            "dedupe_key": job.dedupe_key,
            "started_at": job.started_at,
            "last_heartbeat_at": now,
            "finished_at": job.finished_at or now,
            "duration_sec": duration,
            "pid": os.getpid(),
            "review_status": review_status,
            "error": error,
            "deps_error": deps_error,
        }
        atomic_write_json(review_job_state_path(drive_root, skill_name), payload, trailing_newline=True)
        append_jsonl(
            _events_path(drive_root),
            {
                "ts": now,
                "type": "skill_review_completed" if state_status == "completed" else "skill_review_failed",
                "skill": skill_name,
                "content_hash": payload.get("content_hash", ""),
                "job_id": job.id,
                "duration_sec": duration,
                "status": review_status or state_status,
                "error": error or deps_error,
            },
        )

        has_review_evidence = bool(
            result is not None and (
                getattr(result, "findings", None)
                or getattr(result, "raw_actor_records", None)
            )
        )
        if has_review_evidence:
            try:
                from ouroboros.skill_review import (
                    _count_attempts_for_content,
                    _load_accepted_rebuttals,
                    render_skill_review_block,
                )
                effective_hash = payload.get("content_hash", "") or content_hash
                attempt_idx = _count_attempts_for_content(drive_root, skill_name, effective_hash)
                if attempt_idx <= 0:
                    attempt_idx = 1
                accepted_rebuttals = _load_accepted_rebuttals(drive_root, skill_name)
                markdown = render_skill_review_block(
                    result,
                    attempt_idx=attempt_idx,
                    accepted_rebuttals=accepted_rebuttals,
                )
                append_jsonl(
                    _chat_jsonl_path(drive_root),
                    {
                        "ts": now,
                        "direction": "system",
                        "type": "skill_review",
                        "task_id": "",
                        "skill": skill_name,
                        "status": review_status or state_status,
                        "content_hash": effective_hash,
                        "job_id": job.id,
                        "attempt": attempt_idx,
                        "format": "markdown",
                        "text": markdown,
                    },
                )
            except Exception:
                pass

    return _callback


def _duplicate_payload(skill_name: str, content_hash: str, duplicate: LifecycleJob) -> Dict[str, Any]:
    return {
        "skill": skill_name,
        "status": "pending",
        "content_hash": content_hash,
        "reviewer_models": [],
        "findings": [],
        "error": f"review already {duplicate.status} for this skill/content hash",
        "review_gate": skill_review_gate("pending"),
        "executable_review": False,
        "deps_status": "not_required",
        "deps_error": "",
        "extension_action": None,
        "extension_reason": None,
        "job_id": duplicate.id,
        "job_status": duplicate.status,
    }


def _review_finding_summary(outcome: Any) -> str:
    def _is_pass(item: Dict[str, Any]) -> bool:
        signal = str(item.get("verdict") or item.get("status") or "").strip().lower()
        return signal in {"pass", "passed", "ok"}

    def _chat_headline(text: str, max_chars: int = 180) -> str:
        text = str(text or "").strip()
        if len(text) <= max_chars:
            return text
        marker = "... [omitted {count} chars; full findings in Skills page]"
        budget = max(1, max_chars - len(marker.format(count=0)))
        omitted = max(0, len(text) - budget)
        return text[:budget].rstrip() + marker.format(count=omitted)

    findings = [item for item in (getattr(outcome, "findings", None) or []) if isinstance(item, dict)]
    for item in sorted(findings, key=lambda item: 1 if _is_pass(item) else 0):
        label = str(item.get("item") or item.get("check") or item.get("title") or "finding").strip()
        verdict = str(item.get("verdict") or item.get("severity") or "").strip()
        reason = str(item.get("reason") or item.get("message") or "").strip()
        pieces = [piece for piece in (verdict, label, reason) if piece]
        if pieces:
            summary = ": ".join((" ".join(pieces[:2]), pieces[2])) if len(pieces) > 2 else " ".join(pieces)
            return _chat_headline(summary)
    return ""


def _review_result_message(outcome: Any) -> str:
    status = normalize_skill_review_status(str(getattr(outcome, "status", "") or STATUS_PENDING))
    summary = _review_finding_summary(outcome)
    gate = skill_review_gate(status)
    if gate["executable_review"]:
        prefix = "Review executable with findings" if status in {STATUS_WARNINGS, STATUS_BLOCKERS} else "Review executable"
    elif status == STATUS_WARNINGS:
        prefix = "Review warnings blocked by current enforcement"
    elif status == STATUS_BLOCKERS:
        prefix = "Review blocked: blocker findings"
    else:
        prefix = "Review pending"
    base = f"{prefix} ({status}){f': {summary}' if summary else ''}"
    auto_granted_keys = list(getattr(outcome, "auto_granted_keys", []) or [])
    auto_granted_permissions = list(getattr(outcome, "auto_granted_permissions", []) or [])
    if auto_granted_keys or auto_granted_permissions:
        auto_parts: list[str] = []
        if auto_granted_keys and not auto_granted_permissions:
            auto_parts.append(", ".join(auto_granted_keys))
        elif auto_granted_keys:
            auto_parts.append(f"keys: {', '.join(auto_granted_keys)}")
        if auto_granted_permissions:
            auto_parts.append(f"permissions: {', '.join(auto_granted_permissions)}")
        base = base + f" | auto-granted: {'; '.join(auto_parts)}"
    return base


async def run_skill_review_lifecycle(
    ctx: Any,
    skill_name: str,
    *,
    source: str = "skills",
    review_impl: ReviewImpl = _default_review_skill,
    repo_path: str | None = None,
) -> Dict[str, Any]:
    return await _to_thread_preserving_result(
        run_skill_review_lifecycle_blocking,
        ctx,
        skill_name,
        source=source,
        review_impl=review_impl,
        repo_path=repo_path,
    )


def run_skill_review_lifecycle_blocking(
    ctx: Any,
    skill_name: str,
    *,
    source: str = "tool",
    review_impl: ReviewImpl = _default_review_skill,
    repo_path: str | None = None,
) -> Dict[str, Any]:
    drive_root = pathlib.Path(ctx.drive_root)
    repo_path = repo_path if repo_path is not None else get_skills_repo_path()
    content_hash = _skill_content_hash(drive_root, skill_name, repo_path)
    mark_stale_review_job_interrupted(drive_root, skill_name, current_content_hash=content_hash)
    dedupe_key = _review_dedupe_key(skill_name, content_hash)
    started_monotonic: Dict[str, float] = {}
    progress = JobProgressTarget()

    def _run_review() -> SkillReviewOutcome:
        with _review_job_heartbeat(drive_root, skill_name):
            progress.set("Running tri-model review…")
            outcome = _call_review_with_lifecycle_guard(review_impl, ctx, skill_name)
            deps_status = "not_required"
            deps_error = ""
            executable_review = review_status_allows_execution(getattr(outcome, "status", ""))
            if executable_review:
                progress.set("Installing dependencies…")
                deps_status, deps_error = _reconcile_deps_after_pass_review(
                    drive_root,
                    skill_name,
                    repo_path=repo_path,
                )
            setattr(outcome, "deps_status", deps_status)
            setattr(outcome, "deps_error", deps_error)
            if executable_review and getattr(outcome, "auto_flow", False) and deps_status == "failed":
                outcome.status = STATUS_PENDING
                outcome.error = deps_error or "self-authored dependency reconciliation failed"
                executable_review = False
            if executable_review and getattr(outcome, "auto_flow", False):
                save_enabled(drive_root, skill_name, True)
            progress.set("Reloading extension…")
            extension_action, extension_reason = _reconcile_extension_payload(
                ctx,
                skill_name,
                repo_path=repo_path,
                heal_mode=_heal_mode(ctx),
            )
            setattr(outcome, "extension_action", extension_action)
            setattr(outcome, "extension_reason", extension_reason)
        return outcome

    try:
        outcome = run_lifecycle_job_blocking(
            kind="review",
            target=skill_name,
            source=source,
            message=f"Reviewing {skill_name}",
            dedupe_key=dedupe_key,
            runner=_run_review,
            options=LifecycleJobOptions(
                drive_root=drive_root,
                progress_target=progress,
                result_message=_review_result_message,
                result_error=lambda item: getattr(item, "error", "") or getattr(item, "deps_error", "") or "",
                on_started=_on_started(drive_root, skill_name, content_hash, started_monotonic),
                on_finished=_on_finished(drive_root, skill_name, content_hash, started_monotonic),
            ),
        )
    except DuplicateLifecycleJobError as exc:
        return _duplicate_payload(skill_name, content_hash, exc.job)

    return _outcome_payload(
        outcome,
        deps_status=getattr(outcome, "deps_status", "not_required"),
        deps_error=getattr(outcome, "deps_error", ""),
        extension_action=getattr(outcome, "extension_action", None),
        extension_reason=getattr(outcome, "extension_reason", None),
    )
