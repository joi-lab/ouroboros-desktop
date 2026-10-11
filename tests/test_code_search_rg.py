"""Focused tests for ouroboros.code_search_rg.

Cover the safety/contract behaviors introduced for the search_code worker-OOM
fix: non-regular-file skipping (the /dev pseudo-file read-hang root cause), the
file-scan cap with its explicit "scan stopped" disclosure, the batched rg
invocation (matches collected across the batch_size boundary), and the new
RgSearchResult return contract.
"""
import os
import pathlib
import sys

import pytest

import ouroboros.code_search_rg as rg


def _install_fake_rg(tmp_path, monkeypatch):
    """Install an executable stand-in rg: emits an rg-JSON match per path with the needle."""
    fake = tmp_path / "fake_rg.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "needle = args[args.index('--') + 1]\n"
        "paths = args[args.index('--') + 2:]\n"
        "for p in paths:\n"
        "    try:\n"
        "        text = pathlib.Path(p).read_text(errors='replace')\n"
        "    except Exception:\n"
        "        continue\n"
        "    if needle in text:\n"
        "        line = (text.splitlines() or [''])[0]\n"
        "        print(json.dumps({'type': 'match', 'data': {'path': {'text': p}, 'line_number': 1, 'lines': {'text': line + '\\n'}}}))\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    if os.name == "nt":
        wrapper = tmp_path / "fake_rg.cmd"
        wrapper.write_text(f"@echo off\r\n\"{sys.executable}\" \"{fake}\" %*\r\n", encoding="utf-8")
        target = wrapper
    else:
        target = fake
    monkeypatch.setattr(rg, "_rg_binary", lambda: str(target))


@pytest.mark.skipif(os.name == "nt", reason="os.mkfifo is POSIX-only")
def test_is_search_skippable_rejects_non_regular_files(tmp_path):
    regular = tmp_path / "code.py"
    regular.write_text("x = 1\n", encoding="utf-8")
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    try:
        assert rg.is_search_skippable(regular) is False
        # FIFOs/devices report st_size 0 and read_text() never terminates — must skip.
        assert rg.is_search_skippable(fifo) is True
    finally:
        fifo.unlink()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_is_search_skippable_rejects_symlinks(tmp_path):
    # A symlink inside an allowed root can point outside it; search_code must never
    # follow it (resource-confinement). is_search_skippable rejects symlinks outright.
    outside = tmp_path / "outside.txt"
    outside.write_text("secret needle\n", encoding="utf-8")
    link = tmp_path / "inside_link.txt"
    link.symlink_to(outside)
    assert rg.is_search_skippable(link) is True


def test_format_search_result_surfaces_file_cap_note():
    # No matches + capped scan: the cap MUST be disclosed (else "no matches" misleads).
    empty = rg.RgSearchResult(matches=[], truncated=False, file_capped=True)
    out = rg.format_search_result(
        display_path="active_workspace", root_name="active_workspace",
        root_path=pathlib.Path("/repo"), query="needle",
        regex=False, max_results=200, result=empty,
    )
    assert "scan stopped" in out.lower()


def test_search_with_rg_spans_batch_boundary(tmp_path, monkeypatch):
    # Force a tiny argv budget so each file lands in its own batch; the only match
    # lives in the LAST file, so finding it proves search_with_rg collects matches
    # across batch boundaries. (Driving this via the budget rather than hundreds of
    # files keeps each invocation's argv tiny — safe under the Windows test wrapper's
    # cmd.exe ~8191-char command-line limit.)
    monkeypatch.setattr(rg, "_ARGV_CHAR_BUDGET", 1)
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text(
            "needle here\n" if i == 4 else "nothing\n", encoding="utf-8"
        )
    _install_fake_rg(tmp_path, monkeypatch)
    result = rg.search_with_rg(tmp_path, "needle", regex=False, include="*.txt")
    assert isinstance(result, rg.RgSearchResult)
    assert any(m.path.name == "f4.txt" for m in result.matches)
    assert result.file_capped is False


def test_search_with_rg_caps_file_scan_and_reports(tmp_path, monkeypatch):
    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(rg, "MAX_SEARCH_FILES_SCANNED", 3)
    _install_fake_rg(tmp_path, monkeypatch)
    result = rg.search_with_rg(tmp_path, "needle", regex=False, include="*.txt")
    assert result.file_capped is True  # os.walk stopped at MAX_SEARCH_FILES_SCANNED
    rendered = rg.format_search_result(
        display_path="active_workspace", root_name="active_workspace",
        root_path=tmp_path, query="needle", regex=False, max_results=200, result=result,
    )
    assert "scan stopped" in rendered.lower()
