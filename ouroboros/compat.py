"""
Cross-platform compatibility layer.

Encapsulates all OS-specific operations (process management, file locking,
path conventions) so the rest of the codebase stays platform-agnostic.
"""

from __future__ import annotations

import logging
import os
import pathlib
import platform
import signal
import subprocess
import sys
from typing import Any, List, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Platform flags
# ---------------------------------------------------------------------------
IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

PATH_SEP = ";" if IS_WINDOWS else ":"


# ---------------------------------------------------------------------------
# PID file locking (single-instance guard)
# ---------------------------------------------------------------------------
_lock_fd: Any = None


def pid_lock_acquire(path: str) -> bool:
    """Acquire an exclusive PID lock. Returns True on success."""
    global _lock_fd
    try:
        _lock_fd = open(path, "w")
        if IS_WINDOWS:
            import msvcrt
            msvcrt.locking(_lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
        return True
    except (IOError, OSError):
        return False


def pid_lock_release(path: str) -> None:
    """Release the PID lock."""
    global _lock_fd
    if _lock_fd is not None:
        if IS_WINDOWS:
            import msvcrt
            try:
                msvcrt.locking(_lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
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


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------

def kill_process_tree(proc: subprocess.Popen) -> None:
    """Force-kill a subprocess and its entire process tree."""
    if IS_WINDOWS:
        try:
            subprocess.run(
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


def force_kill_pid(pid: int) -> None:
    """Force-kill a single process by PID."""
    if IS_WINDOWS:
        try:
            subprocess.run(
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


def kill_process_on_port(port: int) -> None:
    """Kill any process listening on the given TCP port."""
    try:
        if IS_WINDOWS:
            res = subprocess.run(
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
                                subprocess.run(
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


# ---------------------------------------------------------------------------
# Embedded Python paths
# ---------------------------------------------------------------------------

def embedded_python_candidates(base_dir: pathlib.Path) -> List[pathlib.Path]:
    """Return candidate paths for the embedded python-build-standalone interpreter."""
    if IS_WINDOWS:
        return [
            base_dir / "python-standalone" / "python.exe",
            base_dir / "python-standalone" / "python3.exe",
        ]
    return [
        base_dir / "python-standalone" / "bin" / "python3",
        base_dir / "python-standalone" / "bin" / "python",
    ]


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


# ---------------------------------------------------------------------------
# Node.js download
# ---------------------------------------------------------------------------

def node_download_info(version: str) -> tuple[str, str, str]:
    """Return (url, extracted_dir_name, archive_type) for Node.js download.

    archive_type is 'zip' for Windows, 'tar.gz' otherwise.
    """
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


# ---------------------------------------------------------------------------
# System profiling helpers
# ---------------------------------------------------------------------------

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
            out = subprocess.check_output(
                ["wmic", "ComputerSystem", "get", "TotalPhysicalMemory", "/value"],
                text=True, timeout=10,
            ).strip()
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
            out = subprocess.check_output(
                ["wmic", "cpu", "get", "Name", "/value"],
                text=True, timeout=10,
            ).strip()
            for line in out.splitlines():
                if "=" in line:
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return platform.processor()


# ---------------------------------------------------------------------------
# Git installation hint
# ---------------------------------------------------------------------------

def git_install_hint() -> str:
    """Return platform-appropriate instructions for installing Git."""
    if IS_MACOS:
        return "Install Git via Xcode CLI Tools: xcode-select --install"
    elif IS_WINDOWS:
        return "Download Git from https://git-scm.com/download/win or run: winget install Git.Git"
    else:
        return "Install Git via your package manager, e.g.: sudo apt install git"
