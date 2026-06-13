"""Cross-platform process, locking, path, and runtime helpers."""

from __future__ import annotations

import logging
import os
import pathlib
import platform
import signal
import subprocess
import sys
import time
from typing import Any, List, Optional

log = logging.getLogger(__name__)

# Platform flags.
IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

PATH_SEP = ";" if IS_WINDOWS else ":"
_SUBPROCESS_NO_WINDOW = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if IS_WINDOWS else 0
)
_PATH_BOOTSTRAPPED = False


def is_container_env() -> bool:
    """Return whether explicit env or Docker sentinel indicates a container."""
    if os.environ.get("OUROBOROS_CONTAINER") == "1":
        return True
    # /.dockerenv is Docker's Linux sentinel.
    if IS_LINUX and pathlib.Path("/.dockerenv").exists():
        return True
    return False


def bootstrap_process_path() -> list[str]:
    """Add existing common user tool directories to this process PATH once."""

    global _PATH_BOOTSTRAPPED
    if _PATH_BOOTSTRAPPED:
        return []
    _PATH_BOOTSTRAPPED = True

    candidates: list[pathlib.Path] = []
    home = pathlib.Path.home()
    if IS_MACOS or IS_LINUX:
        candidates.extend([
            pathlib.Path("/opt/homebrew/bin"),
            pathlib.Path("/opt/homebrew/sbin"),
            pathlib.Path("/usr/local/bin"),
            pathlib.Path("/usr/local/sbin"),
            pathlib.Path("/opt/local/bin"),
            home / ".local" / "bin",
            home / ".cargo" / "bin",
            home / ".npm-global" / "bin",
            home / "go" / "bin",
        ])
    if IS_WINDOWS:
        def _env_path(name: str, default: str = "") -> pathlib.Path | None:
            text = os.environ.get(name, default)
            if not text:
                return None
            path = pathlib.Path(text)
            return path if path.is_absolute() else None

        program_files = _env_path("ProgramFiles", r"C:\Program Files")
        local_app_data = _env_path("LOCALAPPDATA")
        app_data = _env_path("APPDATA")
        user_profile = _env_path("USERPROFILE")
        if program_files:
            candidates.extend([program_files / "Git" / "cmd", program_files / "nodejs"])
        if local_app_data:
            candidates.append(local_app_data / "Programs" / "Git" / "cmd")
        if app_data:
            candidates.append(app_data / "npm")
        if user_profile:
            candidates.append(user_profile / ".cargo" / "bin")

    existing = [part for part in os.environ.get("PATH", "").split(PATH_SEP) if part]
    existing_norm = {str(pathlib.Path(part)).lower() if IS_WINDOWS else str(pathlib.Path(part)) for part in existing}
    added: list[str] = []
    for candidate in candidates:
        try:
            if not candidate.is_dir():
                continue
            text = str(candidate)
            norm = text.lower() if IS_WINDOWS else text
            if norm in existing_norm:
                continue
            existing_norm.add(norm)
            added.append(text)
        except OSError:
            continue
    if added:
        os.environ["PATH"] = PATH_SEP.join([*added, *existing])
    return added


def acquire_exclusive_file_lock(
    lock_path: pathlib.Path,
    *,
    timeout_sec: float = 4.0,
    stale_sec: float = 90.0,
    metadata: str = "",
    poll_sec: float = 0.05,
) -> Optional[int]:
    """Acquire a portable lockfile using O_EXCL and return its file descriptor."""
    lock_path = pathlib.Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    while (time.time() - started) < timeout_sec:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                text = metadata or f"pid={os.getpid()} ts={time.time()}\n"
                os.write(fd, text.encode("utf-8"))
            except Exception:
                log.debug("Failed to write lock metadata to %s", lock_path, exc_info=True)
            return fd
        except (FileExistsError, PermissionError):
            try:
                age = time.time() - lock_path.stat().st_mtime
                if age > stale_sec:
                    lock_path.unlink()
                    continue
            except Exception:
                log.debug("Failed to inspect/remove stale lock %s", lock_path, exc_info=True)
            time.sleep(poll_sec)
        except Exception:
            log.warning("Failed to acquire lock at %s", lock_path, exc_info=True)
            break
    return None


def release_exclusive_file_lock(lock_path: pathlib.Path, lock_fd: Optional[int]) -> None:
    """Release a lock acquired by :func:`acquire_exclusive_file_lock`."""
    lock_path = pathlib.Path(lock_path)
    if lock_fd is None:
        return
    try:
        os.close(lock_fd)
    except Exception:
        log.debug("Failed to close lock fd %s for %s", lock_fd, lock_path, exc_info=True)
    try:
        if lock_path.exists():
            lock_path.unlink()
    except Exception:
        log.debug("Failed to unlink lock file %s", lock_path, exc_info=True)


def unlink_lockfile(lock_path: pathlib.Path) -> None:
    """Best-effort cleanup for path-only locks whose fd was closed after acquire."""
    lock_path = pathlib.Path(lock_path)
    try:
        if lock_path.exists():
            lock_path.unlink()
    except Exception:
        log.debug("Failed to unlink lock file %s", lock_path, exc_info=True)


def open_path_external(path: pathlib.Path) -> None:
    """Open a local path with the platform default application."""

    target = pathlib.Path(path)
    if IS_MACOS:
        subprocess.Popen(["open", str(target)])
    elif IS_WINDOWS:
        os.startfile(str(target))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(target)])


def is_unstable_macos_app_path(path: pathlib.Path) -> bool:
    """Return whether a macOS app path is likely a DMG/AppTranslocation mount."""
    raw = str(path).replace("\\", "/")
    resolved = str(path.resolve()).replace("\\", "/")
    return (
        "AppTranslocation" in raw
        or "AppTranslocation" in resolved
        or raw.startswith("/Volumes/")
        or resolved.startswith("/Volumes/")
    )


def ensure_windows_user_path(path: pathlib.Path) -> None:
    """Add a directory to the current Windows user's PATH and notify shells."""
    if not IS_WINDOWS:
        return
    import winreg  # type: ignore[import-not-found]

    path_text = str(path)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
        try:
            current, value_type = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current, value_type = "", winreg.REG_EXPAND_SZ
        parts = [p for p in str(current).split(";") if p]
        if any(p.lower() == path_text.lower() for p in parts):
            return
        updated = ";".join(parts + [path_text])
        winreg.SetValueEx(key, "Path", 0, value_type, updated)
    _broadcast_windows_environment_change()


def _broadcast_windows_environment_change() -> None:
    if not IS_WINDOWS:
        return
    try:
        import ctypes

        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF,  # HWND_BROADCAST
            0x001A,  # WM_SETTINGCHANGE
            0,
            "Environment",
            0x0002,  # SMTO_ABORTIFHUNG
            5000,
            ctypes.byref(result),
        )
    except Exception:
        pass


def _hidden_run(command: list[str], **kwargs):
    if _SUBPROCESS_NO_WINDOW:
        kwargs = dict(kwargs)
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _SUBPROCESS_NO_WINDOW
    return subprocess.run(command, **kwargs)


# PID file locking.
_lock_fd: Any = None


def pid_lock_acquire(path: str) -> bool:
    """Acquire an exclusive PID lock, closing the fd on lock failure."""
    global _lock_fd
    fd_obj = None
    try:
        fd_obj = open(path, "w")
        if IS_WINDOWS:
            _win32_lock(fd_obj.fileno(), exclusive=True, blocking=False)
        else:
            import fcntl
            fcntl.flock(fd_obj, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd_obj.write(str(os.getpid()))
        fd_obj.flush()
        # Promote to global only after lock and PID write both succeed.
        _lock_fd = fd_obj
        return True
    except (IOError, OSError):
        if fd_obj is not None:
            try:
                fd_obj.close()
            except Exception:
                pass
        return False


def pid_lock_release(path: str) -> None:
    """Release the PID lock."""
    global _lock_fd
    if _lock_fd is not None:
        if IS_WINDOWS:
            try:
                _win32_unlock(_lock_fd.fileno())
            except Exception:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass
        try:
            _lock_fd.close()
        except Exception:
            pass
        _lock_fd = None
    try:
        os.unlink(path)
    except Exception:
        pass


# File locking.

def file_lock_exclusive(fd: int) -> None:
    """Acquire an exclusive (write) lock on a file descriptor. Blocks."""
    if IS_WINDOWS:
        _win32_lock(fd, exclusive=True, blocking=True)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)


def file_lock_shared(fd: int) -> None:
    """Acquire a shared (read) lock on a file descriptor. Blocks."""
    if IS_WINDOWS:
        _win32_lock(fd, exclusive=False, blocking=True)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_SH)


def file_lock_exclusive_nb(fd: int) -> None:
    """Try to acquire an exclusive lock, non-blocking. Raises OSError on failure."""
    if IS_WINDOWS:
        _win32_lock(fd, exclusive=True, blocking=False)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def file_unlock(fd: int) -> None:
    """Release a file lock."""
    if IS_WINDOWS:
        _win32_unlock(fd)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def pid_is_alive(pid: int) -> bool:
    """Return whether a PID appears alive without exposing os.kill to callers."""

    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# Windows file locking via LockFileEx/UnlockFileEx; unlike msvcrt.locking(),
# this works on empty files by locking a range beyond current size.

# Per-fd OVERLAPPED storage for unlock.
_win32_overlapped: dict = {}


_OVERLAPPED_CLS = None  # cached once per process


def _win32_overlapped_class():
    """Return cached portable OVERLAPPED; ctypes requires one class identity."""
    global _OVERLAPPED_CLS
    if _OVERLAPPED_CLS is not None:
        return _OVERLAPPED_CLS

    import ctypes
    from ctypes import wintypes

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _OVERLAPPED_CLS = OVERLAPPED
    return OVERLAPPED


def _win32_lock(fd: int, *, exclusive: bool = True, blocking: bool = True) -> None:
    """Lock a file descriptor using Win32 LockFileEx. Works on empty files."""
    import ctypes
    from ctypes import wintypes
    import msvcrt as _msvcrt

    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002

    OVERLAPPED = _win32_overlapped_class()

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LockFileEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(OVERLAPPED),
    ]
    kernel32.LockFileEx.restype = wintypes.BOOL

    hfile = _msvcrt.get_osfhandle(fd)
    flags = 0
    if exclusive:
        flags |= _LOCKFILE_EXCLUSIVE_LOCK
    if not blocking:
        flags |= _LOCKFILE_FAIL_IMMEDIATELY

    ov = OVERLAPPED()
    # Win32 whole-file lock pattern: huge range from offset 0.
    if not kernel32.LockFileEx(hfile, flags, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(ov)):
        err = ctypes.get_last_error()
        raise OSError(f"LockFileEx failed (error {err})")

    _win32_overlapped[fd] = (hfile, ov)


def _win32_unlock(fd: int) -> None:
    """Unlock a file descriptor previously locked by _win32_lock."""
    import ctypes
    from ctypes import wintypes

    entry = _win32_overlapped.pop(fd, None)
    if entry is None:
        return

    OVERLAPPED = _win32_overlapped_class()

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.UnlockFileEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(OVERLAPPED),
    ]
    kernel32.UnlockFileEx.restype = wintypes.BOOL

    hfile, ov = entry
    try:
        kernel32.UnlockFileEx(hfile, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(ov))
    except OSError:
        pass


# Process management.

def kill_process_tree(proc: subprocess.Popen) -> None:
    """Force-kill a subprocess and its entire process tree."""
    if IS_WINDOWS:
        try:
            _hidden_run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass
    else:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def terminate_process_tree(proc: subprocess.Popen) -> None:
    """Gracefully terminate a subprocess and its process tree."""
    if IS_WINDOWS:
        proc.terminate()
    else:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def terminate_process_group_id(pgid: int) -> None:
    """Gracefully terminate a Unix process group by id."""
    if IS_WINDOWS:
        return
    try:
        os.killpg(int(pgid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError, ValueError):
        pass


def kill_process_group_id(pgid: int) -> None:
    """Force-kill a Unix process group by id."""
    if IS_WINDOWS:
        return
    try:
        os.killpg(int(pgid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError, ValueError):
        pass


def process_group_id(pid: int) -> int:
    """Return the Unix process group id for ``pid`` or 0 when unavailable."""
    if IS_WINDOWS:
        return 0
    try:
        return int(os.getpgid(int(pid)))
    except (ProcessLookupError, PermissionError, OSError, ValueError):
        return 0


def current_process_group_id() -> int:
    """Return the current Unix process group id or 0 when unavailable."""
    if IS_WINDOWS:
        return 0
    try:
        return int(os.getpgrp())
    except (PermissionError, OSError, ValueError):
        return 0


def process_command(pid: int) -> str:
    """Return a best-effort command line for a Unix process."""
    if IS_WINDOWS:
        return ""
    try:
        result = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def force_kill_pid(pid: int) -> None:
    """Force-kill a single process by PID."""
    if IS_WINDOWS:
        try:
            _hidden_run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def kill_pid_tree(pid: int) -> None:
    """Force-kill a PID tree recursively."""
    if IS_WINDOWS:
        try:
            _hidden_run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass
        return

    descendants: list[int] = []
    _collect_descendants(pid, descendants)
    for dpid in reversed(descendants):
        try:
            os.kill(dpid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _collect_descendants(pid: int, result: list[int]) -> None:
    """Recursively collect all descendant PIDs via pgrep."""
    try:
        out = subprocess.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True, text=True, timeout=3,
        )
        for line in out.stdout.strip().splitlines():
            line = line.strip()
            if line:
                child_pid = int(line)
                _collect_descendants(child_pid, result)
                result.append(child_pid)
    except Exception:
        pass


def kill_process_on_port(port: int) -> None:
    """Kill any process listening on the given TCP port."""
    try:
        if IS_WINDOWS:
            res = _hidden_run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=5,
            )
            for line in res.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.strip().split()
                    if parts:
                        try:
                            pid = int(parts[-1])
                            if pid != os.getpid():
                                _hidden_run(
                                    ["taskkill", "/F", "/PID", str(pid)],
                                    capture_output=True,
                                )
                        except (ValueError, ProcessLookupError, PermissionError):
                            pass
        else:
            res = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}"],
                capture_output=True, text=True, timeout=5,
            )
            for pid_str in res.stdout.strip().split():
                try:
                    pid = int(pid_str)
                    if pid != os.getpid():
                        os.kill(pid, 9)
                except (ValueError, ProcessLookupError, PermissionError):
                    pass
    except Exception:
        pass


# Embedded Python paths.

def embedded_python_candidates(base_dir: pathlib.Path) -> List[pathlib.Path]:
    """Return candidate embedded python-build-standalone paths."""
    if IS_WINDOWS:
        return [
            base_dir / "python-standalone" / "python.exe",
            base_dir / "python-standalone" / "python3.exe",
        ]
    return [
        base_dir / "python-standalone" / "bin" / "python3",
        base_dir / "python-standalone" / "bin" / "python",
    ]


def embedded_node_candidates(base_dir: pathlib.Path) -> List[pathlib.Path]:
    """Return candidate bundled Node.js runtime paths."""
    if IS_WINDOWS:
        return [base_dir / "node-standalone" / "node.exe"]
    return [base_dir / "node-standalone" / "bin" / "node"]


def resolve_bundled_node() -> Optional[str]:
    """Return the path to the bundled, signed Node.js runtime if present.

    The packaged app ships an official notarized node under ``node-standalone``
    (re-signed under the hardened runtime by the build's signing pass). Prefer it
    over a PATH (e.g. Homebrew) node, which macOS code-signing enforcement can
    SIGKILL when launched from the packaged process tree.
    """
    bases: List[pathlib.Path] = []
    frozen_base = getattr(sys, "_MEIPASS", None)
    if frozen_base:
        bases.append(pathlib.Path(frozen_base))
    # Dev/source layout: node-standalone sits at the repo root (created by the
    # build scripts), two levels up from this module.
    bases.append(pathlib.Path(__file__).resolve().parent.parent)
    for base in bases:
        for candidate in embedded_node_candidates(base):
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue
    return None


def embedded_pip(base_dir: pathlib.Path) -> Optional[pathlib.Path]:
    """Return path to pip inside embedded python-standalone."""
    if IS_WINDOWS:
        p = base_dir / "python-standalone" / "Scripts" / "pip3.exe"
        if p.exists():
            return p
        p = base_dir / "python-standalone" / "Scripts" / "pip.exe"
        return p if p.exists() else None
    p = base_dir / "python-standalone" / "bin" / "pip3"
    return p if p.exists() else None


# Claude runtime resolution.

from dataclasses import dataclass


@dataclass
class ClaudeRuntimeState:
    """Structured Claude SDK/CLI availability snapshot."""
    # App-managed runtime: bundled SDK and CLI.
    app_managed: bool = False
    sdk_version: str = ""
    sdk_path: str = ""
    cli_path: str = ""
    cli_version: str = ""
    interpreter_path: str = ""

    # Legacy user-site runtime.
    legacy_detected: bool = False
    legacy_sdk_path: str = ""
    legacy_sdk_version: str = ""

    # Operational state.
    ready: bool = False
    api_key_set: bool = False
    error: str = ""
    last_stderr: str = ""

    def status_label(self) -> str:
        if not self.sdk_version:
            return "missing"
        # Version errors must not be shadowed by a missing API key.
        if self.error:
            return "error"
        if not self.api_key_set:
            return "no_api_key"
        if not self.ready:
            return "degraded"
        return "ready"


def _find_sdk_package_path() -> Optional[str]:
    """Return the filesystem path to the installed claude_agent_sdk package."""
    try:
        import claude_agent_sdk
        pkg_file = getattr(claude_agent_sdk, "__file__", None)
        if pkg_file:
            return str(pathlib.Path(pkg_file).parent)
    except ImportError:
        pass
    return None


def _find_bundled_cli(sdk_path: str) -> Optional[str]:
    """Locate the bundled CLI binary inside the SDK package."""
    cli_name = "claude.exe" if IS_WINDOWS else "claude"
    bundled = pathlib.Path(sdk_path) / "_bundled" / cli_name
    if bundled.exists() and bundled.is_file():
        return str(bundled)
    return None


def _probe_cli_version(cli_path: str) -> str:
    """Run ``claude -v`` and return the version string, or empty on failure."""
    try:
        result = subprocess.run(
            [cli_path, "-v"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            import re
            m = re.match(r"([0-9]+\.[0-9]+\.[0-9]+)", result.stdout.strip())
            if m:
                return m.group(1)
    except Exception:
        pass
    return ""


def _detect_legacy_user_site_sdk() -> tuple[bool, str, str]:
    """Detect an SDK installed outside the app-managed python-standalone."""
    sdk_path = _find_sdk_package_path()
    if not sdk_path:
        return False, "", ""
    normalised = pathlib.Path(sdk_path).resolve()
    parts_lower = [p.lower() for p in normalised.parts]
    in_app_bundle = "python-standalone" in parts_lower
    if in_app_bundle:
        return False, "", ""
    try:
        import importlib.metadata
        ver = importlib.metadata.version("claude-agent-sdk")
    except Exception:
        ver = ""
    return True, sdk_path, ver


def resolve_claude_runtime() -> ClaudeRuntimeState:
    """Build a deterministic, non-persistent Claude runtime snapshot."""
    state = ClaudeRuntimeState()
    state.interpreter_path = sys.executable

    # SDK availability.
    try:
        import importlib.metadata
        state.sdk_version = importlib.metadata.version("claude-agent-sdk")
    except Exception:
        pass

    sdk_path = _find_sdk_package_path()
    if sdk_path:
        state.sdk_path = sdk_path

    # App-managed SDK lives inside python-standalone.
    if sdk_path:
        normalised = pathlib.Path(sdk_path).resolve()
        parts_lower = [p.lower() for p in normalised.parts]
        state.app_managed = "python-standalone" in parts_lower

    # Bundled CLI.
    if sdk_path:
        cli = _find_bundled_cli(sdk_path)
        if cli:
            state.cli_path = cli
            state.cli_version = _probe_cli_version(cli)

    # Legacy detection.
    legacy_detected, legacy_path, legacy_ver = _detect_legacy_user_site_sdk()
    state.legacy_detected = legacy_detected
    state.legacy_sdk_path = legacy_path
    state.legacy_sdk_version = legacy_ver

    # API key.
    state.api_key_set = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())

    # Baseline gate avoids false-ready older SDKs with bundled CLI.
    sdk_version_ok = False
    if state.sdk_version:
        try:
            from ouroboros.launcher_bootstrap import _CLAUDE_SDK_MIN_VERSION, _version_tuple
            sdk_version_ok = _version_tuple(state.sdk_version) >= _version_tuple(_CLAUDE_SDK_MIN_VERSION)
        except Exception:
            # Unknown baseline means not-ready so UI offers Repair.
            sdk_version_ok = False
    state.ready = bool(
        state.sdk_version and sdk_version_ok and state.cli_path and state.api_key_set
    )
    if state.sdk_version and not sdk_version_ok and not state.error:
        try:
            from ouroboros.launcher_bootstrap import _CLAUDE_SDK_MIN_VERSION
            state.error = (
                f"Claude SDK {state.sdk_version} is below baseline {_CLAUDE_SDK_MIN_VERSION}. "
                "Run Repair to upgrade."
            )
        except Exception:
            state.error = f"Claude SDK {state.sdk_version} is below the required baseline."

    return state


# Node.js download.

def node_download_info(version: str) -> tuple[str, str, str]:
    """Return ``(url, extracted_dir_name, archive_type)`` for Node.js."""
    arch = platform.machine()
    if IS_WINDOWS:
        na = "x64"
        name = f"node-{version}-win-{na}"
        return f"https://nodejs.org/dist/{version}/{name}.zip", name, "zip"
    elif IS_MACOS:
        na = "arm64" if arch == "arm64" else "x64"
        name = f"node-{version}-darwin-{na}"
        return f"https://nodejs.org/dist/{version}/{name}.tar.gz", name, "tar.gz"
    else:
        na = "arm64" if arch == "aarch64" else "x64"
        name = f"node-{version}-linux-{na}"
        return f"https://nodejs.org/dist/{version}/{name}.tar.gz", name, "tar.gz"


# System profiling helpers.

def get_system_memory() -> str:
    """Return total system memory as a human-readable string."""
    os_name = platform.system()
    try:
        if os_name == "Darwin":
            mem_bytes = int(subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"],
            ).strip())
            return f"{mem_bytes / (1024**3):.1f} GB"
        elif os_name == "Linux":
            out = subprocess.check_output(
                ["awk", '/MemTotal/ {print $2/1024/1024 " GB"}', "/proc/meminfo"],
            ).strip().decode()
            return out
        elif os_name == "Windows":
            out = _hidden_run(
                ["wmic", "ComputerSystem", "get", "TotalPhysicalMemory", "/value"],
                capture_output=True, text=True, timeout=10, check=True,
            ).stdout.strip()
            for line in out.splitlines():
                if "=" in line:
                    mem_bytes = int(line.split("=")[1])
                    return f"{mem_bytes / (1024**3):.1f} GB"
    except Exception:
        pass
    return "Unknown"


def get_cpu_info() -> str:
    """Return CPU model string."""
    os_name = platform.system()
    try:
        if os_name == "Darwin":
            return subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
            ).strip().decode()
        elif os_name == "Windows":
            out = _hidden_run(
                ["wmic", "cpu", "get", "Name", "/value"],
                capture_output=True, text=True, timeout=10, check=True,
            ).stdout.strip()
            for line in out.splitlines():
                if "=" in line:
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return platform.processor()


# Process session isolation.

def create_new_session() -> None:
    """Create a new process session (Unix: setsid). No-op on Windows."""
    if not IS_WINDOWS:
        os.setsid()


def subprocess_new_group_kwargs() -> dict:
    """Return subprocess kwargs for killable process-group/session isolation."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def subprocess_hidden_kwargs() -> dict:
    """Return kwargs to suppress Windows console windows."""
    if IS_WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def merge_hidden_kwargs(kwargs: dict) -> dict:
    """Merge Windows hidden-window flags without dropping caller flags."""
    hidden = subprocess_hidden_kwargs()
    if not hidden:
        return dict(kwargs)
    result = dict(kwargs)
    result["creationflags"] = result.get("creationflags", 0) | hidden.get("creationflags", 0)
    return result


# Git installation hint.

def git_install_hint() -> str:
    """Return platform-appropriate instructions for installing Git."""
    if IS_MACOS:
        return "Install Git via Xcode CLI Tools: xcode-select --install"
    elif IS_WINDOWS:
        return "Download Git from https://git-scm.com/download/win or run: winget install Git.Git"
    else:
        return "Install Git via your package manager, e.g.: sudo apt install git"


# Windows Job Object helpers.

if IS_WINDOWS:
    import ctypes
    import ctypes.wintypes

    _kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    _INVALID_HANDLE_VALUE = ctypes.wintypes.HANDLE(-1)
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JOBOBJECTINFOCLASS_EXTENDED = 9
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SUSPEND_RESUME = 0x0800
    _CREATE_SUSPENDED = 0x4

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.wintypes.DWORD),
            ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
            ("PriorityClass", ctypes.wintypes.DWORD),
            ("SchedulingClass", ctypes.wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _ExtendedLimitInfo(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


def create_kill_on_close_job() -> Optional[Any]:
    """Create a Windows kill-on-close Job Object, or None."""
    if not IS_WINDOWS:
        return None
    try:
        handle = _kernel32.CreateJobObjectW(None, None)
        if handle in (0, _INVALID_HANDLE_VALUE):
            log.warning("CreateJobObjectW failed")
            return None
        info = _ExtendedLimitInfo()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _kernel32.SetInformationJobObject(
            handle,
            _JOBOBJECTINFOCLASS_EXTENDED,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            log.warning("SetInformationJobObject failed")
            _kernel32.CloseHandle(handle)
            return None
        return handle
    except Exception as exc:
        log.warning("Job Object creation failed: %s", exc)
        return None


def assign_pid_to_job(job_handle: Any, pid: int) -> bool:
    """Assign a running process (by PID) to a Job Object. Windows only."""
    if not IS_WINDOWS or job_handle is None:
        return False
    try:
        proc_handle = _kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid,
        )
        if not proc_handle:
            log.warning("OpenProcess(%d) failed for Job Object assignment", pid)
            return False
        ok = _kernel32.AssignProcessToJobObject(job_handle, proc_handle)
        _kernel32.CloseHandle(proc_handle)
        if not ok:
            log.warning("AssignProcessToJobObject failed for pid %d", pid)
            return False
        return True
    except Exception as exc:
        log.warning("Job Object assign failed: %s", exc)
        return False


def terminate_job(job_handle: Any, exit_code: int = 1) -> None:
    """Terminate all processes in a Job Object."""
    if not IS_WINDOWS or job_handle is None:
        return
    try:
        _kernel32.TerminateJobObject(job_handle, exit_code)
    except Exception:
        pass


def close_job(job_handle: Any) -> None:
    """Close a Job Object handle (triggers kill-on-close if set)."""
    if not IS_WINDOWS or job_handle is None:
        return
    try:
        _kernel32.CloseHandle(job_handle)
    except Exception:
        pass


def resume_process(pid: int) -> bool:
    """Resume all threads of a suspended process. Windows only."""
    if not IS_WINDOWS:
        return False
    try:
        _ntdll = ctypes.windll.ntdll  # type: ignore[attr-defined]
        handle = _kernel32.OpenProcess(_PROCESS_SUSPEND_RESUME, False, pid)
        if not handle:
            log.warning("OpenProcess(%d) failed for resume", pid)
            return False
        status = _ntdll.NtResumeProcess(handle)
        _kernel32.CloseHandle(handle)
        if status != 0:
            log.warning("NtResumeProcess(%d) returned NTSTATUS 0x%08x", pid, status)
            return False
        return True
    except Exception as exc:
        log.warning("resume_process failed: %s", exc)
        return False
