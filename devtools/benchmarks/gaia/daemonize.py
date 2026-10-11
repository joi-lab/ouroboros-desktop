#!/usr/bin/env python3.11
"""Generic detacher for multi-hour benchmark runs: double-fork + setsid + caffeinate,
with a ``.DONE`` sentinel written on exit (detached runs get no completion signal).

Usage:  daemonize.py <out_dir> -- <cmd...>
The child's env is inherited, so export GAIA_* / API keys before invoking.

Why: a multi-hour GAIA run must survive (a) the launching shell/tool exiting,
(b) macOS system sleep freezing Docker (``caffeinate -i -s``), and (c) any
parent-process-group teardown (``setsid`` + double-fork). Poll ``<out_dir>/.DONE``.
"""

import os
import subprocess
import sys

out_dir = sys.argv[1]
sep = sys.argv.index("--")
cmd = sys.argv[sep + 1:]
os.makedirs(out_dir, exist_ok=True)
if os.fork() > 0:
    os._exit(0)
os.setsid()
if os.fork() > 0:
    os._exit(0)
lf = open(os.path.join(out_dir, "daemon.log"), "ab")
os.dup2(lf.fileno(), 1)
os.dup2(lf.fileno(), 2)
os.close(0)
rc = subprocess.run(["caffeinate", "-i", "-s"] + cmd, env=dict(os.environ)).returncode
with open(os.path.join(out_dir, ".DONE"), "w") as f:
    f.write(f"EXIT={rc}\n")
