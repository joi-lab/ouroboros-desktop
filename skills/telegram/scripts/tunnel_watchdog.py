"""Hard-death lifeline for the companion's cloudflared process group.

The companion alone owns the pipe write end.  A single ``S`` byte means the
tunnel stopped normally.  EOF or any other byte means the companion vanished
or the protocol was corrupted, so this watchdog kills its own isolated process
group.  Under the Ouroboros companion supervisor that group contains only the
companion, this watchdog, and cloudflared.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys


def run(parent_pid: int, read_fd: int) -> int:
    # Do not require getppid()==parent_pid here: the companion may be SIGKILLed
    # before this new process gets its first timeslice, in which case macOS has
    # already reparented us to launchd.  The isolated process-group identity is
    # the stable authorization check below.
    if parent_pid <= 1 or read_fd < 0:
        return 2
    try:
        with os.fdopen(read_fd, "rb", buffering=0) as lifeline:
            marker = lifeline.read(1)
    except OSError:
        marker = b""
    if marker == b"S":
        return 0

    # The host launches every companion with start_new_session=True.  Refuse a
    # broad group kill if someone manually invokes this helper from a shell.
    if os.getpgrp() == parent_pid:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgrp(), signal.SIGKILL)
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(os.getpid(), signal.SIGKILL)
    return 3


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(2)
    try:
        parent_pid = int(sys.argv[1])
        read_fd = int(sys.argv[2])
    except ValueError:
        raise SystemExit(2) from None
    raise SystemExit(run(parent_pid, read_fd))


if __name__ == "__main__":
    main()
