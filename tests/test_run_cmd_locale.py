"""Regression tests for ``ouroboros.utils.run_cmd`` locale handling.

Without an explicit ``LC_ALL=C`` env, git on a German-locale system returns
German error text and triggers the directory-vs-branch ambiguity error
when a same-named path exists. The fix forces C locale on every
``run_cmd`` subprocess call so:
  (a) error parsing stays English-stable
  (b) ambiguity-resolution heuristics produce predictable output

The maintainer hit this on 2026-05-02 when ``repo_commit`` ran
``git checkout ouroboros`` against a working tree that had both an
``ouroboros/`` directory and an ``ouroboros`` branch — the German
locale error masked the structural problem for ~3 hours.
"""

from __future__ import annotations

import subprocess
from typing import List
from unittest import mock

import pytest

from ouroboros.utils import run_cmd


def test_run_cmd_forces_c_locale_in_subprocess_env():
    """Every ``run_cmd`` call must inject ``LC_ALL``/``LANG``/``LANGUAGE``=C
    so git output is locale-stable across operator environments."""
    captured = {}

    def fake_run(cmd, cwd=None, capture_output=False, text=False, env=None):
        captured["env"] = env
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

    with mock.patch("ouroboros.utils.subprocess.run", side_effect=fake_run):
        out = run_cmd(["git", "status"])

    assert out == "ok"
    env = captured["env"]
    assert env is not None, "run_cmd must always pass an explicit env"
    assert env["LC_ALL"] == "C"
    assert env["LANG"] == "C"
    assert env["LANGUAGE"] == "C"


def test_run_cmd_propagates_other_env_vars():
    """The C-locale injection must NOT erase the rest of the environment.
    Tools that depend on PATH, HOME, etc. must continue to work."""
    captured = {}

    def fake_run(cmd, cwd=None, capture_output=False, text=False, env=None):
        captured["env"] = env
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with mock.patch("ouroboros.utils.subprocess.run", side_effect=fake_run):
        run_cmd(["git", "status"])

    env = captured["env"]
    # PATH should be present (inherited from the test process)
    assert "PATH" in env
    # And LC_ALL is still our injection
    assert env["LC_ALL"] == "C"


def test_run_cmd_raises_on_nonzero_exit():
    """Existing failure semantics must be unchanged: nonzero exit code
    raises RuntimeError with stdout + stderr in the message."""
    def fake_run(cmd, cwd=None, capture_output=False, text=False, env=None):
        return subprocess.CompletedProcess(
            cmd, 128, stdout="some stdout", stderr="some stderr",
        )

    with mock.patch("ouroboros.utils.subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError) as excinfo:
            run_cmd(["git", "fail"])

    assert "Command failed" in str(excinfo.value)
    assert "some stdout" in str(excinfo.value)
    assert "some stderr" in str(excinfo.value)


def test_run_cmd_passes_cwd_when_provided(tmp_path):
    """``cwd`` must be passed to subprocess.run as a string, not a Path."""
    captured = {}

    def fake_run(cmd, cwd=None, capture_output=False, text=False, env=None):
        captured["cwd"] = cwd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with mock.patch("ouroboros.utils.subprocess.run", side_effect=fake_run):
        run_cmd(["git", "status"], cwd=tmp_path)

    assert captured["cwd"] == str(tmp_path)


def test_run_cmd_cwd_omitted_when_none():
    captured = {}

    def fake_run(cmd, cwd=None, capture_output=False, text=False, env=None):
        captured["cwd"] = cwd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with mock.patch("ouroboros.utils.subprocess.run", side_effect=fake_run):
        run_cmd(["git", "status"])

    assert captured["cwd"] is None


def test_repo_commit_checkout_uses_disambiguator():
    """``tools/git.py::_repo_commit_push`` must call
    ``run_cmd(["git", "checkout", branch, "--"])`` — the trailing
    ``--`` resolves the directory-vs-branch ambiguity that wedged
    ``ouroboros/`` (the directory) against ``ouroboros`` (the branch)."""
    import pathlib
    src = pathlib.Path(__file__).parent.parent / "ouroboros" / "tools" / "git.py"
    body = src.read_text(encoding="utf-8")
    # The two checkout call sites must include the trailing "--".
    matches = body.count('run_cmd(["git", "checkout", ctx.branch_dev, "--"]')
    assert matches >= 2, (
        f"Expected at least 2 disambiguated checkout call sites in "
        f"tools/git.py, found {matches}. The fix must apply to all "
        f"``git checkout ctx.branch_dev`` callers."
    )
    # And the un-disambiguated form must not appear.
    assert 'run_cmd(["git", "checkout", ctx.branch_dev], cwd' not in body, (
        "An un-disambiguated ``git checkout ctx.branch_dev`` callsite "
        "still exists in tools/git.py. All such calls must include the "
        "trailing ``--`` to prevent the directory-vs-branch ambiguity."
    )
