"""Tests for the 2026-05-04 tool-name-as-argv[0] hint in ``run_shell``.

qwen3-coder-30b emits malformed run_shell calls of the shape::

    {"name": "run_shell",
     "arguments": {"cmd": ["run_shell", "cd /x && y"]}}

Subprocess returns ``ENOENT`` on the literal "run_shell"; the resulting
``[Errno 2] No such file or directory: 'run_shell'`` gives the model no
recovery signal, so it loops on the same shape (observed in production
task daa1d6ce, 3 identical malformed calls).

The boundary catches the malformation, surfaces a structured hint, and
suggests the right argv form.
"""

from __future__ import annotations

from types import SimpleNamespace

from ouroboros.tools.shell import _run_shell


def _ctx(tmp_path):
    return SimpleNamespace(repo_dir=tmp_path)


def test_run_shell_as_argv0_with_shell_metachars_suggests_bash_c(tmp_path):
    """The exact failure shape we observed: tool name + bash-string."""
    ctx = _ctx(tmp_path)
    result = _run_shell(
        ctx,
        ["run_shell", "cd /Users/roble/development/ouroboros-desktop && git status --porcelain"],
    )
    assert "SHELL_ARG_ERROR" in result
    assert "tool name" in result
    assert "bash" in result and "-c" in result


def test_run_shell_as_argv0_without_metachars_drops_tool_name(tmp_path, monkeypatch):
    """Plain argv with the tool name accidentally prepended — suggest dropping it."""
    ctx = _ctx(tmp_path)
    result = _run_shell(ctx, ["run_shell", "git", "status"])
    assert "SHELL_ARG_ERROR" in result
    assert "drop the tool name" in result
    assert "git" in result and "status" in result


def test_shell_as_argv0_also_caught(tmp_path):
    """``shell`` is a common alias the model emits as well."""
    ctx = _ctx(tmp_path)
    result = _run_shell(ctx, ["shell", "ls -la"])
    assert "SHELL_ARG_ERROR" in result
    assert "tool name" in result


def test_real_binary_argv0_proceeds_to_existing_checks(tmp_path, monkeypatch):
    """``cd`` is a real shell builtin and must still hit the existing
    builtin-rejection path, not the new tool-name hint."""
    ctx = _ctx(tmp_path)
    result = _run_shell(ctx, ["cd", "/tmp"])
    assert "SHELL_CMD_ERROR" in result
    assert '"cd" is a shell builtin' in result


def test_lone_tool_name_with_no_rest_still_caught(tmp_path):
    """Edge case: ``cmd=["run_shell"]`` — the hint fires without the
    'Likely fix' line because there's nothing to fix into."""
    ctx = _ctx(tmp_path)
    result = _run_shell(ctx, ["run_shell"])
    assert "SHELL_ARG_ERROR" in result
    assert "tool name" in result
