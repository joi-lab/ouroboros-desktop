"""Native filesystem controls for delegated evidence publication; run on Windows CI.

A capture's evidence parent is created without writing through a junction (a
private directory). The old cell about later evidence generations preserving
protected earlier destinations went with the generations' copy: a writer's
checkout is released once its capture and evidence are saved."""
from __future__ import annotations

import os

import pytest

from ouroboros import delegate_named_evidence as named

pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires native Windows attributes, sharing and junctions")


def test_capture_parent_junction_is_refused_without_touching_its_target(tmp_path):
    import _winapi

    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    _winapi.CreateJunction(str(outside), str(alias))
    with pytest.raises(OSError, match="private directory"):
        named.private_directory(alias / "files", tmp_path)
    assert not (outside / "files").exists()
    named.private_directory(tmp_path / "ordinary" / "files", tmp_path)
    assert (tmp_path / "ordinary" / "files").is_dir()
