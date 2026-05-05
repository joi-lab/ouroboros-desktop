"""Tests for the 2026-05-05 ``OUROBOROS_BRANCH_DEV`` env override.

Pre-fix, ``server._runtime_branch_defaults()`` always returned the
upstream defaults ``("ouroboros", "ouroboros-stable")`` (or whatever
the launcher-managed git_ops module reported when running under
supervisor). Forks working on a different branch (e.g.
``local-first-patches``) had every ``repo_commit`` /
``repo_write_commit`` fail at the ``git checkout <branch_dev> --``
step because checkout to the upstream branch hit divergent history.

The fix reads ``OUROBOROS_BRANCH_DEV`` and ``OUROBOROS_BRANCH_STABLE``
from the environment (settings.json populates these via
``apply_settings_to_env``). Empty values fall through to the existing
defaults so upstream installations are unaffected.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def server_module(monkeypatch):
    """Import server.py with _LAUNCHER_MANAGED forced False so the
    test never reaches the supervisor.git_ops branch (which depends on
    runtime state we don't have in unit tests)."""
    import server as _srv
    monkeypatch.setattr(_srv, "_LAUNCHER_MANAGED", False, raising=False)
    return _srv


def test_default_returns_upstream_branches_when_env_unset(server_module, monkeypatch):
    """Empty env = historical defaults. Upstream installs unaffected."""
    monkeypatch.delenv("OUROBOROS_BRANCH_DEV", raising=False)
    monkeypatch.delenv("OUROBOROS_BRANCH_STABLE", raising=False)
    dev, stable = server_module._runtime_branch_defaults()
    assert dev == "ouroboros"
    assert stable == "ouroboros-stable"


def test_env_override_branch_dev(server_module, monkeypatch):
    """Setting OUROBOROS_BRANCH_DEV in env wins."""
    monkeypatch.setenv("OUROBOROS_BRANCH_DEV", "local-first-patches")
    monkeypatch.delenv("OUROBOROS_BRANCH_STABLE", raising=False)
    dev, stable = server_module._runtime_branch_defaults()
    assert dev == "local-first-patches"
    # Stable falls through to default since the env var is unset.
    assert stable == "ouroboros-stable"


def test_env_override_both_branches(server_module, monkeypatch):
    """Both env vars together — fork drives can name both."""
    monkeypatch.setenv("OUROBOROS_BRANCH_DEV", "feature/x")
    monkeypatch.setenv("OUROBOROS_BRANCH_STABLE", "main")
    dev, stable = server_module._runtime_branch_defaults()
    assert dev == "feature/x"
    assert stable == "main"


def test_empty_env_value_treated_as_unset(server_module, monkeypatch):
    """Empty string in env means "not set" — fall through to default.
    This matches the SETTINGS_DEFAULTS shape where the default value is
    the empty string, signalling "use upstream default"."""
    monkeypatch.setenv("OUROBOROS_BRANCH_DEV", "")
    monkeypatch.setenv("OUROBOROS_BRANCH_STABLE", "")
    dev, stable = server_module._runtime_branch_defaults()
    assert dev == "ouroboros"
    assert stable == "ouroboros-stable"


def test_whitespace_only_value_treated_as_unset(server_module, monkeypatch):
    """Whitespace-only env values are stripped and treated as empty —
    avoids 'branch named "  "' bugs from accidental settings.json
    formatting."""
    monkeypatch.setenv("OUROBOROS_BRANCH_DEV", "   ")
    dev, _ = server_module._runtime_branch_defaults()
    assert dev == "ouroboros"


def test_settings_keys_in_apply_settings_to_env_envkeys():
    """``apply_settings_to_env`` must include both branch keys in its
    env_keys list, otherwise settings.json values for them will be
    silently ignored after the first save cycle (the standing footgun
    documented in CLAUDE.md)."""
    config_src = open("ouroboros/config.py").read()
    assert '"OUROBOROS_BRANCH_DEV"' in config_src, (
        "OUROBOROS_BRANCH_DEV must appear in ouroboros/config.py "
        "(SETTINGS_DEFAULTS or env_keys list)"
    )
    assert '"OUROBOROS_BRANCH_STABLE"' in config_src, (
        "OUROBOROS_BRANCH_STABLE must appear in ouroboros/config.py"
    )
    # Both must be in env_keys (not just SETTINGS_DEFAULTS), or
    # apply_settings_to_env won't push them into os.environ.
    env_keys_start = config_src.index("env_keys = [")
    env_keys_end = config_src.index("]", env_keys_start)
    env_keys_block = config_src[env_keys_start:env_keys_end]
    assert "OUROBOROS_BRANCH_DEV" in env_keys_block, (
        "OUROBOROS_BRANCH_DEV must be in apply_settings_to_env's env_keys list"
    )
    assert "OUROBOROS_BRANCH_STABLE" in env_keys_block, (
        "OUROBOROS_BRANCH_STABLE must be in apply_settings_to_env's env_keys list"
    )
