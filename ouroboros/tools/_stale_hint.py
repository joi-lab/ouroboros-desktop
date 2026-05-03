"""Tool-error suffix that surfaces stale-module situations.

When a tool call fails and the supervisor's source tree has been edited
since the worker booted, append a one-line hint so the agent can self-
diagnose the cascade — instead of escalating to OS-level workarounds
like the directory-rename catastrophe documented on 2026-05-02.

Use
---
.. code:: python

    err_msg = f"⚠️ GIT_ERROR (checkout): {sanitize(str(e))}"
    return _stale_hint.maybe_append(err_msg)

Idempotent: if the hint is already in ``msg``, a second call leaves
``msg`` unchanged. Safe to call from multiple error paths in one task.

The function is also a no-op when:
    - the boot fingerprint was never set (e.g. dev-mode bypass),
    - the source tree is in fact fresh,
    - the kill-switch ``OUROBOROS_STALE_MODULE_HINT=false`` is set,
    - the watch roots are missing (frozen builds).
"""

from __future__ import annotations

import os
import pathlib
from typing import Optional

# Module-level imports so tests can monkeypatch the helper functions.
# These are pure-stdlib + supervisor modules; no circular-import risk.
from supervisor.module_watcher import default_watch_roots, diagnose_staleness
from supervisor.state import (
    get_boot_module_baseline,
    get_boot_module_fingerprint,
)

_HINT_MARKER = "STALE_MODULE_HINT"


def maybe_append(msg: str, *, repo_dir: Optional[pathlib.Path] = None) -> str:
    """Return ``msg`` with a stale-module hint suffix when the runtime has
    drifted from its on-disk source. No-op otherwise."""
    if not msg:
        return msg
    if _HINT_MARKER in msg:
        return msg
    if os.environ.get("OUROBOROS_STALE_MODULE_HINT", "true").strip().lower() in ("false", "0", "no"):
        return msg

    boot_fp = get_boot_module_fingerprint()
    if not boot_fp:
        return msg

    if repo_dir is None:
        # Fall back to the env value the launcher sets.
        env_repo = os.environ.get("OUROBOROS_REPO_DIR", "")
        if env_repo:
            repo_dir = pathlib.Path(env_repo)

    if repo_dir is None:
        return msg

    try:
        roots = default_watch_roots(pathlib.Path(repo_dir))
        if not roots:
            return msg
        report = diagnose_staleness(
            boot_fp, roots, boot_baseline=get_boot_module_baseline(),
        )
    except Exception:
        return msg

    if not report.is_stale:
        return msg

    suffix = report.hint_sentence(repo_dir=pathlib.Path(repo_dir))
    if not suffix:
        return msg
    return f"{msg}\n\n{suffix}"
