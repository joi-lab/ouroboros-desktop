"""Process-control helpers for the self-editable server entrypoint."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
from typing import Any


def restart_current_process(host: str, port: int, *, repo_dir: pathlib.Path, log: Any) -> None:
    env = os.environ.copy()
    desired_host = str(host)
    try:
        from ouroboros.config import load_settings
        desired_host = (
            str(os.environ.get("OUROBOROS_SERVER_HOST") or "").strip()
            or str(load_settings().get("OUROBOROS_SERVER_HOST") or "").strip()
            or desired_host
        )
    except Exception:
        desired_host = str(host)
    env["OUROBOROS_SERVER_HOST"] = desired_host
    env["OUROBOROS_SERVER_PORT"] = str(port)
    env.pop("OUROBOROS_MANAGED_BY_LAUNCHER", None)
    argv = [sys.executable, *sys.argv]
    log.info("Re-executing direct server mode on %s:%d", desired_host, port)
    try:
        os.execvpe(sys.executable, argv, env)
    except Exception:
        log.exception("Direct re-exec failed; attempting spawned restart fallback.")
        try:
            subprocess.Popen(argv, env=env, cwd=str(repo_dir))
            log.info("Spawned replacement server process after exec failure.")
        except Exception:
            log.exception("Spawned restart fallback failed; exiting with restart code only.")


def execute_panic_stop(
    consciousness: Any,
    kill_workers_fn,
    *,
    data_dir: pathlib.Path,
    panic_exit_code: int,
    log: Any,
) -> None:
    """Full emergency stop: kill everything, write panic flag, hard-exit."""
    log.critical("PANIC STOP initiated.")
    try:
        consciousness.stop()
    except Exception:
        pass

    try:
        from supervisor.state import load_state, save_state

        st = load_state()
        st["evolution_mode_enabled"] = False
        st["bg_consciousness_enabled"] = False
        save_state(st)
    except Exception:
        pass

    try:
        panic_flag = data_dir / "state" / "panic_stop.flag"
        panic_flag.parent.mkdir(parents=True, exist_ok=True)
        panic_flag.write_text("panic", encoding="utf-8")
    except Exception:
        pass

    try:
        from ouroboros.local_model import get_manager

        get_manager().stop_server()
    except Exception:
        pass

    try:
        from ouroboros.tools.shell import kill_all_tracked_subprocesses

        kill_all_tracked_subprocesses()
    except Exception:
        pass

    try:
        from ouroboros.tools.services import kill_all_services

        kill_all_services(wait=False)
    except Exception:
        pass

    try:
        from ouroboros.extension_companion import panic_kill_all

        panic_kill_all()
    except Exception:
        pass

    try:
        kill_workers_fn(force=True, archive_service_logs=False)
    except Exception:
        pass

    try:
        import multiprocessing
        from ouroboros.gateway.host_service import host_service_port
        from ouroboros.platform_layer import force_kill_pid, kill_process_on_port

        for child in multiprocessing.active_children():
            try:
                force_kill_pid(child.pid)
            except (ProcessLookupError, PermissionError):
                pass
        kill_process_on_port(8765)
        kill_process_on_port(8766)
        kill_process_on_port(host_service_port())
    except Exception:
        pass

    log.critical("PANIC STOP complete — hard exit with code %d.", panic_exit_code)
    os._exit(panic_exit_code)
