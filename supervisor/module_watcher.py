"""
Module-watcher — restart workers when supervisor/agent source files change.

Background
==========
On 2026-05-02 the agent ("Ouro") spent ~3 hours stuck in a deadlock cascade
because a fix to ``supervisor/git_ops.py`` was applied to disk while the
running supervisor process had the OLD module cached in ``sys.modules``.
The fix was invisible to the live agent. He then escalated to OS-level
workarounds (renaming the ``ouroboros/`` directory) which broke the
frozen-contract safety and required manual recovery.

This module prevents that cascade structurally: poll a fixed set of
source-file roots each supervisor-loop tick, and when any tracked file's
mtime advances past the baseline, synthesize a ``restart_request`` event
into the existing supervisor event queue. The launcher's existing
``RESTART_EXIT_CODE = 42`` machinery then handles the rest — workers
restart, modules reload from disk, the agent picks up the patch.

Design notes
============
- Pure stdlib (no ``watchdog``/``pyinotify`` dependency).
- Polling is cheap: a few dozen ``os.stat`` calls per tick, sub-millisecond.
- A 2-second debounce coalesces multi-file edits (e.g. patching two
  files at once) into a single restart event.
- A 90-second boot grace prevents restart loops if the watcher itself
  is the change that just landed.
- Frozen builds (PyInstaller ``sys.frozen``) early-return ``[]`` from
  ``default_watch_roots`` because ``supervisor/`` doesn't exist on
  disk in that mode.
- Master switch ``OUROBOROS_AUTO_RESTART_ON_MODULE_CHANGE`` (default
  ``true``) flips the watcher to no-op for emergencies.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import sys
import time
from typing import Dict, Iterable, List, Optional, Tuple


# Don't trigger immediately at boot — give the supervisor 90 seconds to
# settle. Mirrors ``supervisor.workers._SPAWN_GRACE_SEC`` semantics.
_BOOT_GRACE_SEC = 90.0

# Coalesce multi-file edits into a single restart event.
_DEFAULT_DEBOUNCE_SEC = 2.0

# File patterns to skip (build artifacts, caches, editor temp files).
_IGNORE_BASENAMES = frozenset({
    "__pycache__", ".DS_Store", ".pytest_cache", ".mypy_cache",
})
_IGNORE_SUFFIXES = (".pyc", ".pyo", ".swp", ".swo", "~")


@dataclasses.dataclass(frozen=True)
class ModuleDriftEvent:
    """Surface payload describing a detected source-tree change."""
    changed_paths: Tuple[str, ...]
    first_seen_ts: float

    def summary_sentence(self) -> str:
        n = len(self.changed_paths)
        if n == 0:
            return "no changed files"
        if n == 1:
            return f"1 file changed: {self.changed_paths[0]}"
        head = ", ".join(self.changed_paths[:3])
        tail = f" (+{n - 3} more)" if n > 3 else ""
        return f"{n} files changed: {head}{tail}"


@dataclasses.dataclass(frozen=True)
class StalenessReport:
    """Result of comparing a boot-time fingerprint to the current source tree.

    Used by tool-error formatters to distinguish "the agent's code is wrong"
    from "the runtime is using stale modules whose on-disk version has been
    patched." Emitted as a hint suffix on failure messages so the agent can
    self-diagnose the cascade without escalating to OS-level workarounds.
    """
    changed_paths: Tuple[str, ...]
    boot_fingerprint: str
    current_fingerprint: str

    @property
    def is_stale(self) -> bool:
        return self.boot_fingerprint != self.current_fingerprint

    def hint_sentence(self, *, repo_dir: Optional[pathlib.Path] = None) -> str:
        """One-line operator-readable hint suitable for appending to a tool
        error message. Renders relative paths when ``repo_dir`` is provided."""
        if not self.is_stale:
            return ""
        n = len(self.changed_paths)
        paths = list(self.changed_paths)
        if repo_dir is not None:
            try:
                repo = pathlib.Path(repo_dir).resolve()
                paths = [
                    str(pathlib.Path(p).resolve().relative_to(repo))
                    if pathlib.Path(p).resolve().is_relative_to(repo)  # py>=3.9
                    else p
                    for p in paths
                ]
            except Exception:
                pass
        head = ", ".join(paths[:3])
        tail = f" (+{n - 3} more)" if n > 3 else ""
        return (
            f"⚠️ STALE_MODULE_HINT: {n} file(s) changed on disk since this "
            f"process started: {head}{tail}. The error above may reflect "
            f"the OLD code in memory, not the current source. If "
            f"OUROBOROS_AUTO_RESTART_ON_MODULE_CHANGE=true, a restart is "
            f"queued; otherwise ask the operator to restart."
        )


def compute_fingerprint(roots: Iterable[pathlib.Path]) -> str:
    """Stable SHA1 hash of the source-tree fingerprint.

    Same input tree always produces the same hex digest regardless of OS
    scandir order. Useful as a single string identifier to embed in
    ``worker_boot`` events for cross-task drift comparison.
    """
    import hashlib
    import json as _json
    fp = fingerprint(roots)
    blob = _json.dumps(fp, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()


def diagnose_staleness(
    boot_fingerprint: str,
    roots: Iterable[pathlib.Path],
    *,
    boot_baseline: Optional[Dict[str, int]] = None,
) -> StalenessReport:
    """Compare a stored boot-time fingerprint against the current source tree.

    Returns a ``StalenessReport``. ``is_stale`` is the canonical "should I
    surface a hint?" predicate. ``changed_paths`` is empty when fresh; when
    stale and ``boot_baseline`` is supplied (the original ``{path: mtime}``
    dict from boot), per-path drift is computed; otherwise ``changed_paths``
    is empty even on stale.
    """
    current = fingerprint(roots)
    current_hash = compute_fingerprint(roots) if current else ""
    if current_hash == boot_fingerprint:
        return StalenessReport(
            changed_paths=(),
            boot_fingerprint=boot_fingerprint,
            current_fingerprint=current_hash,
        )
    changed: List[str] = []
    if boot_baseline is not None:
        old_keys = set(boot_baseline.keys())
        new_keys = set(current.keys())
        changed.extend(new_keys - old_keys)
        changed.extend(old_keys - new_keys)
        for k in old_keys & new_keys:
            if boot_baseline[k] != current[k]:
                changed.append(k)
    return StalenessReport(
        changed_paths=tuple(sorted(changed)),
        boot_fingerprint=boot_fingerprint,
        current_fingerprint=current_hash,
    )


def default_watch_roots(repo_dir: pathlib.Path) -> List[pathlib.Path]:
    """Default set of source roots to watch.

    Returns the empty list under PyInstaller frozen builds — those package
    Python source into a self-extracting bundle, so disk-edits are not a
    realistic input there.
    """
    if getattr(sys, "frozen", False):
        return []
    repo = pathlib.Path(repo_dir)
    candidates = [
        repo / "supervisor",
        repo / "ouroboros",
        repo / "server.py",
    ]
    return [p for p in candidates if p.exists()]


def _iter_files(roots: Iterable[pathlib.Path]) -> Iterable[pathlib.Path]:
    """Yield every ``.py`` file under any root, ignoring build artifacts."""
    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # Mutate dirnames in-place to prune ignored dirs from the walk.
            dirnames[:] = [d for d in dirnames if d not in _IGNORE_BASENAMES]
            for name in filenames:
                if name in _IGNORE_BASENAMES:
                    continue
                if name.endswith(_IGNORE_SUFFIXES):
                    continue
                if not name.endswith(".py"):
                    continue
                yield pathlib.Path(dirpath) / name


def fingerprint(roots: Iterable[pathlib.Path]) -> Dict[str, int]:
    """Return ``{relpath: mtime_ns}`` for every tracked file.

    Sorted-key-stable: same input tree always produces the same dict
    regardless of OS-level scandir order. Callers can hash this dict
    deterministically with ``json.dumps(sort_keys=True)``.
    """
    out: Dict[str, int] = {}
    for path in _iter_files(roots):
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            continue
        out[str(path)] = mtime_ns
    return dict(sorted(out.items()))


class ModuleWatcher:
    """Polling source-tree watcher with debounce + boot-grace.

    Usage
    -----
    .. code:: python

        watcher = ModuleWatcher(default_watch_roots(REPO_DIR))
        watcher.baseline()
        # ... in supervisor main loop:
        evt = watcher.check()
        if evt is not None:
            event_q.put({"type": "restart_request", "reason": "module_drift",
                         "changed_paths": list(evt.changed_paths)})
    """

    def __init__(
        self,
        roots: Iterable[pathlib.Path],
        *,
        debounce_sec: float = _DEFAULT_DEBOUNCE_SEC,
        boot_grace_sec: float = _BOOT_GRACE_SEC,
        clock=time.time,
    ) -> None:
        self._roots = tuple(pathlib.Path(r) for r in roots)
        self._debounce_sec = float(debounce_sec)
        self._boot_grace_sec = float(boot_grace_sec)
        self._clock = clock
        self._baseline: Dict[str, int] = {}
        self._boot_ts: float = 0.0
        # First-seen wallclock for a change set we haven't yet fired on.
        # Used to enforce the debounce window.
        self._pending_first_seen_ts: Optional[float] = None

    def baseline(self) -> None:
        """Capture the current source-tree state. Call once at supervisor
        boot before entering the main loop."""
        self._baseline = fingerprint(self._roots)
        self._boot_ts = self._clock()
        self._pending_first_seen_ts = None

    def check(self) -> Optional[ModuleDriftEvent]:
        """Poll once. Returns a drift event when:
          1. The supervisor has been up at least ``boot_grace_sec``,
          2. AND at least one tracked file's mtime differs from the baseline,
          3. AND the change has been observed for at least ``debounce_sec``
             continuously (i.e. the file isn't still being written to).

        Returns ``None`` otherwise (no drift, or drift is too fresh).
        """
        if not self._roots:
            return None
        now = self._clock()
        if now - self._boot_ts < self._boot_grace_sec:
            return None

        current = fingerprint(self._roots)
        if current == self._baseline:
            # No drift — clear any pending debounce state and return.
            self._pending_first_seen_ts = None
            return None

        # Drift detected. Either start the debounce clock, or check
        # whether enough time has passed to fire. With ``debounce_sec=0``,
        # fire on the first detection — useful in tests and for operators
        # who want zero-latency restart on disk-edit.
        if self._pending_first_seen_ts is None:
            self._pending_first_seen_ts = now
            if self._debounce_sec > 0:
                return None

        if now - self._pending_first_seen_ts < self._debounce_sec:
            return None

        # Fire. Compute the path delta for telemetry.
        changed = self._diff_paths(self._baseline, current)
        first_seen = self._pending_first_seen_ts
        # Roll the baseline forward so subsequent ticks don't re-fire on
        # the same change. The supervisor will exit-restart shortly anyway,
        # but rolling forward keeps semantics correct for tests and for
        # the case where the operator has set the master switch off.
        self._baseline = current
        self._pending_first_seen_ts = None
        return ModuleDriftEvent(
            changed_paths=tuple(sorted(changed)),
            first_seen_ts=first_seen,
        )

    @staticmethod
    def _diff_paths(old: Dict[str, int], new: Dict[str, int]) -> List[str]:
        """Return paths that differ between two fingerprints (added,
        removed, or mtime-changed)."""
        old_keys = set(old.keys())
        new_keys = set(new.keys())
        diff: List[str] = []
        diff.extend(new_keys - old_keys)  # added
        diff.extend(old_keys - new_keys)  # removed
        for k in old_keys & new_keys:
            if old[k] != new[k]:
                diff.append(k)
        return diff
