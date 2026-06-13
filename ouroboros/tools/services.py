"""Task-scoped long-running service manager."""

from __future__ import annotations

import json
import os
import pathlib
import re
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List

from ouroboros.observability import redact_projection, write_blob
from ouroboros.platform_layer import (
    bootstrap_process_path,
    kill_process_group_id,
    kill_process_tree,
    process_group_id,
    subprocess_new_group_kwargs,
)
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tool_access import resolve_shell_cwd
from ouroboros.utils import append_jsonl, utc_now_iso


@dataclass
class ServiceRecord:
    name: str
    service_id: str
    task_id: str
    cmd: List[str]
    cwd: str
    log_path: pathlib.Path
    proc: subprocess.Popen
    pgid: int = 0
    started_at: float = field(default_factory=time.time)
    readiness: Dict[str, Any] = field(default_factory=dict)
    ready: bool = False
    outputs: List[str] = field(default_factory=list)
    cwd_root: str = ""
    before_outputs: Dict[str, tuple[bool, int, str]] = field(default_factory=dict)


_LOCK = threading.Lock()
_SERVICES: Dict[str, ServiceRecord] = {}
_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_MAX_SERVICE_LOG_BLOB_BYTES = 5_000_000
_MAX_SERVICE_LOG_TAIL_CHARS = 80_000


def _service_key(ctx: ToolContext, name: str) -> str:
    task_id = str(getattr(ctx, "task_id", "") or "manual")
    return f"{task_id}:{name}"


def _tail(path: pathlib.Path, chars: int) -> str:
    if not path.exists():
        return ""
    size = path.stat().st_size
    limit = max(0, int(chars))
    with path.open("rb") as fh:
        fh.seek(max(0, size - limit))
        data = fh.read(limit)
    return data.decode("utf-8", errors="replace")


def _sanitize_service_name(name: str) -> tuple[str, str]:
    service_name = str(name or "service").strip() or "service"
    if not _SERVICE_NAME_RE.fullmatch(service_name):
        return "", "⚠️ TOOL_ARG_ERROR (start_service): name must match [A-Za-z0-9_.-]{1,80}."
    return service_name, ""


def _readiness_timeout(readiness: Dict[str, Any] | None) -> tuple[float, str]:
    raw = (readiness or {}).get("timeout_sec", 5)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0, "⚠️ TOOL_ARG_ERROR (start_service): readiness.timeout_sec must be numeric."
    if value < 0:
        return 0.0, "⚠️ TOOL_ARG_ERROR (start_service): readiness.timeout_sec must be non-negative."
    return min(value, 25.0), ""


def _service_env() -> Dict[str, str]:
    allowed_exact = {
        "PATH",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "NODE_PATH",
        "SystemRoot",
        "SYSTEMROOT",
        "WINDIR",
        "windir",
        "COMSPEC",
        "ComSpec",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "NUMBER_OF_PROCESSORS",
        "PROGRAMDATA",
        "ProgramData",
        "ProgramFiles",
        "PROGRAMFILES",
        "ProgramFiles(x86)",
        "PROGRAMFILES(X86)",
    }
    allowed_casefold = {key.casefold() for key in allowed_exact}
    env: Dict[str, str] = {}
    for key, value in os.environ.items():
        if key.casefold() not in allowed_casefold and not key.startswith("LC_"):
            continue
        try:
            redacted = redact_projection(str(value))
            if redacted.records:
                continue
        except Exception:
            continue
        env[key] = str(value)
    return env


def _stop_record(record: ServiceRecord, *, wait: bool = True) -> None:
    if record.pgid:
        kill_process_group_id(record.pgid)
    elif record.proc.poll() is None:
        kill_process_tree(record.proc)
    if not wait:
        return
    try:
        record.proc.wait(timeout=5)
    except Exception:
        pass


def _finalize_service_log_for_drive(drive_root: pathlib.Path, record: ServiceRecord) -> Dict[str, Any]:
    result: Dict[str, Any] = {"deleted_live_log": False, "full_log_ref": {}, "tail": "", "errors": []}
    log_path = record.log_path
    try:
        size = log_path.stat().st_size if log_path.exists() else 0
        result["tail"] = str(redact_projection(_tail(log_path, _MAX_SERVICE_LOG_TAIL_CHARS)).value)
        if size <= _MAX_SERVICE_LOG_BLOB_BYTES:
            text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
            result["full_log_ref"] = write_blob(pathlib.Path(drive_root), text, kind="txt")
        else:
            result["full_log_omitted"] = f"log exceeds {_MAX_SERVICE_LOG_BLOB_BYTES} byte blob cap"
    except Exception as exc:
        result["errors"].append(f"capture: {type(exc).__name__}: {exc}")
    should_delete = bool((result.get("full_log_ref") or {}).get("sha256")) or not log_path.exists()
    if should_delete:
        try:
            log_path.unlink(missing_ok=True)
            result["deleted_live_log"] = True
            parent = log_path.parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except Exception as exc:
            result["errors"].append(f"delete: {type(exc).__name__}: {exc}")
    elif log_path.exists():
        result["retained_live_log_path"] = str(log_path)
    return result


def _finalize_service_log(ctx: ToolContext, record: ServiceRecord) -> Dict[str, Any]:
    return _finalize_service_log_for_drive(pathlib.Path(ctx.drive_root), record)


def _archive_stale_service_log(
    drive_root: pathlib.Path,
    log_path: pathlib.Path,
    *,
    event_type: str = "service_log_pruned",
) -> Dict[str, Any]:
    result: Dict[str, Any] = {"archived": False, "deleted_live_log": False, "full_log_ref": {}, "errors": []}
    try:
        size = log_path.lstat().st_size
        if size > _MAX_SERVICE_LOG_BLOB_BYTES:
            result["retained_live_log_path"] = str(log_path)
            result["full_log_omitted"] = f"log exceeds {_MAX_SERVICE_LOG_BLOB_BYTES} byte blob cap"
            return result
        text = log_path.read_text(encoding="utf-8", errors="replace")
        result["full_log_ref"] = write_blob(pathlib.Path(drive_root), text, kind="txt")
        result["tail_chars"] = len(str(redact_projection(_tail(log_path, _MAX_SERVICE_LOG_TAIL_CHARS)).value))
        append_jsonl(pathlib.Path(drive_root) / "logs" / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": event_type,
            "task_id": log_path.parent.name,
            "name": log_path.stem,
            "full_log_ref": result["full_log_ref"],
            "tail_chars": result["tail_chars"],
        })
        result["archived"] = True
        log_path.unlink(missing_ok=True)
        result["deleted_live_log"] = True
    except Exception as exc:
        result["errors"].append(f"{log_path}: {type(exc).__name__}: {exc}")
    return result


def archive_task_service_logs(
    drive_root: pathlib.Path,
    task_id: str,
    task: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Archive and remove leftover live service logs for a terminal task."""

    task_text = str(task_id or "").strip()
    if isinstance(task, dict):
        seen: set[str] = set()
        roots: List[pathlib.Path] = []
        for candidate in (
            drive_root,
            task.get("drive_root"),
            task.get("child_drive_root"),
            task.get("headless_child_drive_root"),
        ):
            if not candidate:
                continue
            root = pathlib.Path(candidate).resolve(strict=False)
            key = str(root)
            if key in seen:
                continue
            seen.add(key)
            roots.append(root)
        reports = [archive_task_service_logs(root, task_text) for root in roots]
        return {
            "task_id": task_text,
            "roots": [str(root) for root in roots],
            "archived_files": sum(int(report.get("archived_files") or 0) for report in reports),
            "deleted_files": sum(int(report.get("deleted_files") or 0) for report in reports),
            "deleted_dirs": sum(int(report.get("deleted_dirs") or 0) for report in reports),
            "retained_files": sum(int(report.get("retained_files") or 0) for report in reports),
            "errors": [err for report in reports for err in (report.get("errors") or [])],
        }
    report = {
        "task_id": task_text,
        "archived_files": 0,
        "deleted_files": 0,
        "deleted_dirs": 0,
        "retained_files": 0,
        "errors": [],
    }
    if not task_text or pathlib.Path(task_text).name != task_text:
        report["errors"].append("invalid task_id")
        return report
    task_dir = pathlib.Path(drive_root) / "services" / task_text
    try:
        task_stat = task_dir.lstat()
    except OSError:
        return report
    if not stat.S_ISDIR(task_stat.st_mode):
        return report
    try:
        for child in task_dir.glob("*.log"):
            try:
                child_stat = child.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(child_stat.st_mode):
                continue
            archive_result = _archive_stale_service_log(
                pathlib.Path(drive_root),
                child,
                event_type="service_log_archived",
            )
            if archive_result.get("archived"):
                report["archived_files"] += 1
            if archive_result.get("deleted_live_log"):
                report["deleted_files"] += 1
            if archive_result.get("retained_live_log_path"):
                report["retained_files"] += 1
            report["errors"].extend(archive_result.get("errors") or [])
        if task_dir.exists() and not any(task_dir.iterdir()):
            task_dir.rmdir()
            report["deleted_dirs"] += 1
    except Exception as exc:
        report["errors"].append(f"{task_dir}: {type(exc).__name__}: {exc}")
    return report


def _refresh_ready(record: ServiceRecord) -> bool:
    if record.proc.poll() is not None:
        record.ready = False
        return False
    readiness = record.readiness or {}
    contains = str(readiness.get("stdout_contains") or readiness.get("log_contains") or "").strip()
    if not contains:
        record.ready = True
        return True
    record.ready = contains in _tail(record.log_path, 20_000)
    return record.ready


def _start_service(
    ctx: ToolContext,
    cmd: List[str],
    name: str = "service",
    cwd: str = "",
    readiness: Dict[str, Any] | None = None,
    outputs: List[str] | None = None,
) -> str:
    if not isinstance(cmd, list) or not cmd or not all(str(x).strip() for x in cmd):
        return "⚠️ TOOL_ARG_ERROR (start_service): cmd must be a non-empty array of strings."
    service_name, name_error = _sanitize_service_name(name)
    if name_error:
        return name_error
    readiness_timeout, readiness_error = _readiness_timeout(readiness)
    if readiness_error:
        return readiness_error
    key = _service_key(ctx, service_name)
    with _LOCK:
        existing = _SERVICES.get(key)
        if existing and existing.proc.poll() is None:
            return f"⚠️ SERVICE_ALREADY_RUNNING: {service_name} pid={existing.proc.pid}"
    try:
        workdir, cwd_root, _allowed_roots = resolve_shell_cwd(ctx, cwd, operation="service")
        workdir = pathlib.Path(workdir).resolve(strict=False)
    except Exception as exc:
        return f"⚠️ SERVICE_CWD_ERROR: {type(exc).__name__}: {exc}"
    declared_outputs = [str(item) for item in (outputs or []) if str(item or "").strip()]
    try:
        from ouroboros.tools.shell import _snapshot_declared_outputs

        before_outputs = _snapshot_declared_outputs(ctx, declared_outputs, workdir, cwd_root=cwd_root)
    except Exception:
        before_outputs = {}
    task_id = str(getattr(ctx, "task_id", "") or "manual")
    log_dir = pathlib.Path(ctx.drive_root) / "services" / task_id
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{service_name}.log"
    log_fh = log_path.open("ab")
    try:
        bootstrap_process_path()
        proc = subprocess.Popen(
            [str(part) for part in cmd],
            cwd=str(workdir),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=_service_env(),
            **subprocess_new_group_kwargs(),
        )
        pgid = process_group_id(proc.pid)
        log_fh.close()
    except Exception as exc:
        log_fh.close()
        return f"⚠️ SERVICE_START_ERROR: {type(exc).__name__}: {exc}"
    record = ServiceRecord(
        name=service_name,
        service_id=key,
        task_id=task_id,
        cmd=[str(part) for part in cmd],
        cwd=str(workdir),
        log_path=log_path,
        proc=proc,
        pgid=pgid,
        readiness=dict(readiness or {}),
        outputs=declared_outputs,
        cwd_root=cwd_root,
        before_outputs=before_outputs,
    )
    with _LOCK:
        _SERVICES[key] = record
    try:
        if cwd_root == "active_workspace" and not bool(getattr(ctx, "is_workspace_mode", lambda: False)()):
            from ouroboros.tools.commit_gate import _invalidate_advisory

            _invalidate_advisory(
                ctx,
                changed_paths=[f"<service:{service_name}>"],
                mutation_root=workdir,
                source_tool="start_service",
            )
    except Exception:
        pass
    deadline = time.time() + readiness_timeout
    while time.time() < deadline:
        if _refresh_ready(record):
            break
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    return json.dumps(_status_payload(record), ensure_ascii=False, indent=2)


def _status_payload(record: ServiceRecord) -> Dict[str, Any]:
    _refresh_ready(record)
    rc = record.proc.poll()
    state = "running" if rc is None else "exited"
    return {
        "service_id": record.service_id,
        "name": record.name,
        "task_id": record.task_id,
        "pid": record.proc.pid,
        "state": state,
        "ready": bool(record.ready),
        "returncode": rc,
        "uptime_sec": round(max(0.0, time.time() - record.started_at), 3),
        "cwd": record.cwd,
        "cwd_root": record.cwd_root,
        "cmd": record.cmd,
        "outputs": list(record.outputs),
        "log_path": str(record.log_path),
        "ts": utc_now_iso(),
    }


def _service_status(ctx: ToolContext, name: str = "service") -> str:
    service_name, name_error = _sanitize_service_name(name)
    if name_error:
        return name_error
    key = _service_key(ctx, service_name)
    with _LOCK:
        record = _SERVICES.get(key)
    if not record:
        return f"⚠️ SERVICE_NOT_FOUND: {name}"
    return json.dumps(_status_payload(record), ensure_ascii=False, indent=2)


def _service_logs(ctx: ToolContext, name: str = "service", tail: int = 8000) -> str:
    service_name, name_error = _sanitize_service_name(name)
    if name_error:
        return name_error
    key = _service_key(ctx, service_name)
    with _LOCK:
        record = _SERVICES.get(key)
    if not record:
        return f"⚠️ SERVICE_NOT_FOUND: {name}"
    try:
        tail_chars = int(tail or 8000)
    except (TypeError, ValueError):
        return "⚠️ TOOL_ARG_ERROR (service_logs): tail must be an integer."
    tail_chars = min(max(1, tail_chars), _MAX_SERVICE_LOG_TAIL_CHARS)
    text = str(redact_projection(_tail(record.log_path, tail_chars)).value)
    ref = {}
    omitted_reason = ""
    try:
        size = record.log_path.stat().st_size if record.log_path.exists() else 0
        if size <= _MAX_SERVICE_LOG_BLOB_BYTES:
            full = record.log_path.read_text(encoding="utf-8", errors="replace") if record.log_path.exists() else ""
            ref = write_blob(pathlib.Path(ctx.drive_root), full, kind="txt")
        else:
            omitted_reason = f"log exceeds {_MAX_SERVICE_LOG_BLOB_BYTES} byte blob cap"
    except Exception:
        ref = {}
    return json.dumps({
        "service_id": record.service_id,
        "name": record.name,
        "tail": text,
        "full_log_ref": ref,
        "full_log_omitted": omitted_reason,
    }, ensure_ascii=False, indent=2)


def _stop_service(ctx: ToolContext, name: str = "service") -> str:
    service_name, name_error = _sanitize_service_name(name)
    if name_error:
        return name_error
    key = _service_key(ctx, service_name)
    with _LOCK:
        record = _SERVICES.pop(key, None)
    if not record:
        return f"⚠️ SERVICE_NOT_FOUND: {name}"
    _stop_record(record)
    payload = _status_payload(record)
    payload["log_finalization"] = _finalize_service_log(ctx, record)
    artifact_note = ""
    artifact_failed = False
    if record.outputs:
        try:
            from ouroboros.tools.shell import _register_process_outputs

            artifact_note, artifact_failed = _register_process_outputs(
                ctx,
                record.outputs,
                pathlib.Path(record.cwd),
                cwd_root=record.cwd_root,
                before_outputs=record.before_outputs,
            )
        except Exception as exc:
            artifact_note = f"\n\n⚠️ ARTIFACT_OUTPUT_ERROR:\n- service output finalization failed: {type(exc).__name__}: {exc}"
            artifact_failed = True
    elif record.cwd_root == "user_files":
        payload["artifact_audit_gap"] = (
            "⚠️ ARTIFACT_AUDIT_GAP: service ran in user_files cwd without outputs=[...]. "
            "If it created a deliverable, rerun/register the file with outputs or "
            "write_file(root=artifact_store) before claiming it."
        )
    if artifact_note:
        payload["artifact_outputs"] = artifact_note.strip()
    payload["artifact_output_failed"] = bool(artifact_failed)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if artifact_failed:
        return "⚠️ ARTIFACT_OUTPUT_ERROR (stop_service): declared service outputs were not finalized.\n\n" + rendered
    return rendered


def stop_task_services(ctx: ToolContext) -> List[Dict[str, Any]]:
    task_id = str(getattr(ctx, "task_id", "") or "manual")
    stopped: List[Dict[str, Any]] = []
    with _LOCK:
        keys = [
            key for key, record in _SERVICES.items()
            if record.task_id == task_id
        ]
    for key in keys:
        name = key.split(":", 1)[1]
        try:
            raw = _stop_service(ctx, name=name)
            if raw.startswith("⚠️ ARTIFACT_OUTPUT_ERROR") and "\n\n{" in raw:
                raw = raw.split("\n\n", 1)[1]
            payload = json.loads(raw)
            stopped.append(payload)
        except Exception:
            pass
    return stopped


def kill_all_services(
    drive_root: pathlib.Path | None = None,
    *,
    wait: bool = True,
) -> List[Dict[str, Any]]:
    """Stop every tracked service process group for panic/shutdown paths."""

    with _LOCK:
        records = list(_SERVICES.values())
        _SERVICES.clear()
    stopped: List[Dict[str, Any]] = []
    for record in records:
        _stop_record(record, wait=wait)
        payload = _status_payload(record)
        if wait and drive_root is not None:
            payload["log_finalization"] = _finalize_service_log_for_drive(pathlib.Path(drive_root), record)
        stopped.append(payload)
    if wait and drive_root is not None and stopped:
        def _compact(payload: Dict[str, Any]) -> Dict[str, Any]:
            item = dict(payload)
            finalization = dict(item.get("log_finalization") or {})
            tail = finalization.pop("tail", "")
            if tail:
                finalization["tail_chars"] = len(str(tail))
            item["log_finalization"] = finalization
            return item

        try:
            append_jsonl(pathlib.Path(drive_root) / "logs" / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "services_shutdown_cleanup",
                "services": [_compact(payload) for payload in stopped],
            })
        except Exception:
            pass
    return stopped


def prune_service_logs(
    drive_root: pathlib.Path,
    retention_days: int | None = None,
    *,
    now: float | None = None,
) -> Dict[str, Any]:
    if retention_days is None:
        try:
            from ouroboros.config import SETTINGS_DEFAULTS
            default_days = str(SETTINGS_DEFAULTS.get("OUROBOROS_SERVICE_LOG_RETENTION_DAYS") or 14)
        except Exception:
            default_days = "14"
        raw = os.environ.get("OUROBOROS_SERVICE_LOG_RETENTION_DAYS", default_days).strip() or default_days
        try:
            retention_days = int(raw)
        except ValueError:
            return {"enabled": False, "deleted_dirs": 0, "deleted_files": 0, "errors": [f"invalid retention days: {raw!r}"]}
    retention_days = max(1, min(int(retention_days), 365))
    cutoff = (time.time() if now is None else float(now)) - retention_days * 86400
    services_root = pathlib.Path(drive_root) / "services"
    report = {
        "enabled": True,
        "retention_days": retention_days,
        "deleted_dirs": 0,
        "deleted_files": 0,
        "archived_files": 0,
        "retained_files": 0,
        "errors": [],
    }
    if not services_root.exists():
        return report
    for task_dir in list(services_root.iterdir()):
        try:
            task_stat = task_dir.lstat()
        except OSError:
            continue
        if not stat.S_ISDIR(task_stat.st_mode):
            continue
        try:
            for child in task_dir.glob("*.log"):
                try:
                    child_stat = child.lstat()
                except OSError:
                    continue
                if stat.S_ISREG(child_stat.st_mode) and child_stat.st_mtime < cutoff:
                    archive_result = _archive_stale_service_log(pathlib.Path(drive_root), child)
                    if archive_result.get("archived"):
                        report["archived_files"] += 1
                    if archive_result.get("deleted_live_log"):
                        report["deleted_files"] += 1
                    if archive_result.get("retained_live_log_path"):
                        report["retained_files"] += 1
                    report["errors"].extend(archive_result.get("errors") or [])
            if not any(task_dir.iterdir()):
                task_dir.rmdir()
                report["deleted_dirs"] += 1
        except Exception as exc:
            report["errors"].append(f"{task_dir}: {type(exc).__name__}: {exc}")
    return report


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("start_service", {
            "name": "start_service",
            "description": "Start a task-scoped long-running service and return pid/readiness/state.",
            "parameters": {"type": "object", "properties": {
                "cmd": {"type": "array", "items": {"type": "string"}},
                "cwd": {"type": "string", "default": ""},
                "name": {"type": "string", "default": "service"},
                "readiness": {"type": "object", "default": {}, "description": "Optional {log_contains|stdout_contains, timeout_sec} readiness probe."},
                "outputs": {"type": "array", "items": {"type": "string"}, "default": [], "description": "Files generated by the service to copy into the task artifact store when the service stops."},
            }, "required": ["cmd"]},
        }, _start_service, is_code_tool=True, timeout_sec=30),
        ToolEntry("service_status", {
            "name": "service_status",
            "description": "Return pid/state/readiness/uptime for a task-scoped service.",
            "parameters": {"type": "object", "properties": {
                "name": {"type": "string", "default": "service"},
            }, "required": []},
        }, _service_status),
        ToolEntry("service_logs", {
            "name": "service_logs",
            "description": "Return bounded service log tail plus a private full-log blob ref.",
            "parameters": {"type": "object", "properties": {
                "name": {"type": "string", "default": "service"},
                "tail": {"type": "integer", "default": 8000},
            }, "required": []},
        }, _service_logs),
        ToolEntry("stop_service", {
            "name": "stop_service",
            "description": "Stop a task-scoped service process group.",
            "parameters": {"type": "object", "properties": {
                "name": {"type": "string", "default": "service"},
            }, "required": []},
        }, _stop_service, is_code_tool=True, timeout_sec=30),
    ]
