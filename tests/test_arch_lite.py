"""Tests for the ARCHITECTURE-LITE injection in sparse prompt mode.

The lite doc is a curated self-portrait that ships in sparse-mode
block 0 to give the agent passive structural self-knowledge — without
the ~170K-char full ARCHITECTURE.md that sparse mode drops.

These tests pin the wiring so a refactor can't silently lose the
sparse-mode-only injection or the kill-switch.
"""

from __future__ import annotations

import pathlib

import pytest


_REPO_ROOT = pathlib.Path(__file__).parent.parent
_LITE_PATH = _REPO_ROOT / "docs" / "ARCHITECTURE-LITE.md"


# ---------------------------------------------------------------------------
# File presence + shape
# ---------------------------------------------------------------------------

def test_arch_lite_file_exists():
    assert _LITE_PATH.exists(), (
        f"docs/ARCHITECTURE-LITE.md must exist for sparse mode to inject it. "
        f"Expected at {_LITE_PATH}."
    )


def test_arch_lite_is_compact():
    """Hard cap: must stay under 30K chars. The whole point is to be a
    LITE alternative to the 170K-char full ARCHITECTURE.md. If this test
    starts failing, prune the file rather than raising the cap."""
    size = _LITE_PATH.stat().st_size
    assert size <= 30_000, (
        f"ARCHITECTURE-LITE.md is {size} chars; must stay <= 30000. "
        f"If you need more space, the full ARCHITECTURE.md is the right home."
    )


def test_arch_lite_documents_authority_order():
    """The lite doc must explicitly say BIBLE.md and identity.md override it.
    Without this, the file becomes a competing self-narrative."""
    body = _LITE_PATH.read_text(encoding="utf-8").lower()
    assert "bible" in body and "identity" in body, (
        "ARCHITECTURE-LITE.md must reference BIBLE and identity. The point "
        "is to support — not replace — the constitutional layer."
    )


def test_arch_lite_acknowledges_full_arch():
    """The lite doc must point at the full ARCHITECTURE.md so the agent
    knows where to go when he needs deeper detail."""
    body = _LITE_PATH.read_text(encoding="utf-8")
    assert "ARCHITECTURE.md" in body, (
        "ARCHITECTURE-LITE.md must reference the full ARCHITECTURE.md as "
        "the authoritative source."
    )


# ---------------------------------------------------------------------------
# Settings + env propagation
# ---------------------------------------------------------------------------

def test_settings_default_true():
    from ouroboros.config import SETTINGS_DEFAULTS
    assert SETTINGS_DEFAULTS.get("OUROBOROS_ARCH_LITE_ENABLED") is True, (
        "OUROBOROS_ARCH_LITE_ENABLED must default to True so sparse-mode "
        "users get the self-portrait without explicit opt-in."
    )


def test_settings_propagated_to_env():
    """The key must be in apply_settings_to_env's env_keys list, otherwise
    a settings.json change wouldn't reach the worker process."""
    import inspect
    from ouroboros import config
    src = inspect.getsource(config.apply_settings_to_env)
    assert "OUROBOROS_ARCH_LITE_ENABLED" in src, (
        "OUROBOROS_ARCH_LITE_ENABLED must appear in apply_settings_to_env's "
        "env_keys list so settings.json changes propagate to workers."
    )


# ---------------------------------------------------------------------------
# Sparse-mode injection (via context module surface, not full prompt build)
# ---------------------------------------------------------------------------

def test_context_module_references_arch_lite_path():
    """Pin the file path used in the injection. If we ever rename the file
    we want this test to scream so the rename gets propagated."""
    src = (_REPO_ROOT / "ouroboros" / "context.py").read_text(encoding="utf-8")
    assert "docs/ARCHITECTURE-LITE.md" in src
    assert "arch_lite_md" in src or "ARCH_LITE" in src, (
        "context.py must read ARCHITECTURE-LITE.md when building the "
        "sparse-mode static block. Look for arch_lite_md."
    )


def test_context_gates_lite_on_sparse_only():
    """The lite doc must NOT inject under medium or dense — those modes
    already carry the full ARCH, and double-loading is wasted budget."""
    src = (_REPO_ROOT / "ouroboros" / "context.py").read_text(encoding="utf-8")
    # Look for the sparse gate near the arch_lite assignment
    assert "if sparse and" in src or "sparse:" in src, (
        "Injection logic must condition on sparse mode."
    )


def test_context_kill_switch_recognized():
    """OUROBOROS_ARCH_LITE_ENABLED=false must skip the read."""
    src = (_REPO_ROOT / "ouroboros" / "context.py").read_text(encoding="utf-8")
    assert "OUROBOROS_ARCH_LITE_ENABLED" in src, (
        "context.py must read the OUROBOROS_ARCH_LITE_ENABLED env var "
        "to honor the kill switch."
    )


def test_context_static_block_includes_lite_heading():
    """The injected block must have a recognizable heading so the agent
    can locate it inside the static block."""
    src = (_REPO_ROOT / "ouroboros" / "context.py").read_text(encoding="utf-8")
    assert "ARCHITECTURE-LITE.md" in src and "self-portrait" in src.lower(), (
        "Static-block insertion must use a clear heading like "
        "'## docs/ARCHITECTURE-LITE.md (self-portrait)'."
    )


def test_context_heading_carries_full_path():
    """The heading MUST include the docs/ prefix so the agent can repo_read
    the file without having to guess the path. Bare-basename heading led
    to repeated ENOENT failures in production (2026-05-03)."""
    src = (_REPO_ROOT / "ouroboros" / "context.py").read_text(encoding="utf-8")
    assert "## docs/ARCHITECTURE-LITE.md" in src, (
        "Heading must include 'docs/' prefix so repo_read targets the right "
        "path. Without it the agent guesses the repo root and hits ENOENT."
    )


def test_sparse_tail_includes_arch_lite_path_breadcrumb():
    """The sparse-mode explanatory tail must teach the path explicitly so
    the agent can re-fetch the lite block on demand."""
    src = (_REPO_ROOT / "ouroboros" / "context.py").read_text(encoding="utf-8")
    assert "repo_read('docs/ARCHITECTURE-LITE.md')" in src, (
        "Sparse-mode tail must include the breadcrumb "
        "repo_read('docs/ARCHITECTURE-LITE.md') so the agent knows where "
        "the file lives, parallel to the existing breadcrumb for full ARCH."
    )
