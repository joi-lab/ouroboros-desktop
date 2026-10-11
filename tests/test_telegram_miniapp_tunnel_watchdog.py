from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


WATCHDOG_PATH = Path(__file__).parents[1] / "skills" / "telegram" / "scripts" / "tunnel_watchdog.py"
SPEC = importlib.util.spec_from_file_location("telegram_poc_tunnel_watchdog", WATCHDOG_PATH)
assert SPEC and SPEC.loader
watchdog = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watchdog)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX watchdog contract")
def test_watchdog_normal_marker_never_kills_group(monkeypatch: pytest.MonkeyPatch) -> None:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"S")
    os.close(write_fd)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(watchdog.os, "getppid", lambda: 1234)
    monkeypatch.setattr(watchdog.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
    assert watchdog.run(1234, read_fd) == 0
    assert killed == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX watchdog contract")
def test_watchdog_eof_kills_only_expected_isolated_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    killed: list[tuple[int, int]] = []
    # The companion may already be gone before watchdog gets its first
    # timeslice; reparenting to launchd must not turn EOF into a fail-open exit.
    monkeypatch.setattr(watchdog.os, "getppid", lambda: 1)
    monkeypatch.setattr(watchdog.os, "getpgrp", lambda: 1234)
    monkeypatch.setattr(watchdog.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
    assert watchdog.run(1234, read_fd) == 3
    assert killed == [(1234, signal.SIGKILL)]


@pytest.mark.skipif(sys.platform != "darwin", reason="PoC watchdog targets macOS")
def test_hard_companion_death_reaps_watchdog_and_tunnel_process(tmp_path: Path) -> None:
    helper = tmp_path / "hard_death_helper.py"
    helper.write_text(
        """
import json, os, subprocess, sys, time
watchdog_path = sys.argv[1]
read_fd, write_fd = os.pipe()
os.set_inheritable(write_fd, False)
watchdog = subprocess.Popen(
    [sys.executable, watchdog_path, str(os.getpid()), str(read_fd)],
    pass_fds=(read_fd,), stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
os.close(read_fd)
tunnel = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
print(json.dumps({"watchdog": watchdog.pid, "tunnel": tunnel.pid}), flush=True)
time.sleep(60)
""".lstrip(),
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, str(helper), str(WATCHDOG_PATH)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert process.stdout is not None
        ready, _write, _error = select.select([process.stdout], [], [], 5)
        assert ready, "isolated helper did not publish child PIDs"
        children = json.loads(process.stdout.readline())
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=5)

        def serving(pid: int) -> bool:
            status = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            ).stdout.strip()
            return bool(status) and not status.startswith("Z")

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(serving(int(pid)) for pid in children.values()):
            time.sleep(0.05)
        assert not serving(int(children["watchdog"]))
        assert not serving(int(children["tunnel"]))
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)
