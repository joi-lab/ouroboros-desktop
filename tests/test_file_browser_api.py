"""Tests for the extracted file browser API."""

from __future__ import annotations

import asyncio
import json
import pathlib
from io import BytesIO

import pytest
from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.testclient import TestClient

import ouroboros.gateway.files as file_browser_api


def _make_client(root: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("OUROBOROS_FILE_BROWSER_DEFAULT", str(root))
    return TestClient(Starlette(routes=file_browser_api.file_browser_routes()))


def test_network_requests_require_explicit_file_root(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OUROBOROS_FILE_BROWSER_DEFAULT", raising=False)

    with TestClient(Starlette(routes=file_browser_api.file_browser_routes())) as client:
        response = client.get("/api/files/list")

    assert response.status_code == 400
    assert "OUROBOROS_FILE_BROWSER_DEFAULT" in response.json()["error"]


def test_external_symlink_file_is_listed_but_not_accessible(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (root / "inside.txt").write_text("ok", encoding="utf-8")

    escape = root / "escape.txt"
    try:
        escape.symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation is not available in this test environment.")

    with _make_client(root, monkeypatch) as client:
        list_response = client.get("/api/files/list?path=.")
        assert list_response.status_code == 200
        entries = {entry["name"]: entry for entry in list_response.json()["entries"]}
        names = list(entries)
        assert "inside.txt" in names
        assert "escape.txt" in names
        assert entries["escape.txt"]["is_symlink"] is True

        # v6.26.0 containment contract: a symlink RESOLVING outside the root is
        # listed (the link itself lives in-root) but cannot be read, written,
        # or deleted through the API — that was a root escape.
        read_response = client.get("/api/files/read?path=escape.txt")
        assert read_response.status_code == 400
        assert "escapes file browser root" in read_response.json()["error"]

        write_response = client.post(
            "/api/files/write",
            json={"path": "escape.txt", "content": "changed"},
        )
        assert write_response.status_code == 400
        assert outside.read_text(encoding="utf-8") == "secret"

        delete_response = client.post("/api/files/delete", json={"path": "escape.txt"})
        assert delete_response.status_code == 400
        assert escape.is_symlink()
        assert outside.exists()


def test_external_symlink_dir_mutations_are_blocked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
):
    root = tmp_path / "root"
    root.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    copy_dir = root / "copies"
    copy_dir.mkdir()

    external_dir = root / "external-dir"
    try:
        external_dir.symlink_to(outside_dir, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation is not available in this test environment.")

    outside_file = outside_dir / "note.txt"
    outside_file.write_text("hello", encoding="utf-8")
    external_file = root / "external-file.txt"
    try:
        external_file.symlink_to(outside_file)
    except OSError:
        pytest.skip("Symlink creation is not available in this test environment.")

    with _make_client(root, monkeypatch) as client:
        # v6.26.0 containment contract: mutations THROUGH an out-of-root
        # symlink are blocked (they were a root escape).
        mkdir_response = client.post(
            "/api/files/mkdir",
            json={"path": "external-dir", "name": "child"},
        )
        assert mkdir_response.status_code == 400
        assert not (outside_dir / "child").exists()

        copy_response = client.post(
            "/api/files/transfer",
            json={
                "source_path": "external-file.txt",
                "destination_dir": "copies",
                "mode": "copy",
            },
        )
        assert copy_response.status_code == 400
        assert not (copy_dir / "external-file.txt").exists()

    class _Client:
        host = "testclient"

    class _UploadRequest:
        client = _Client()

        async def form(self):
            return {
                "path": "external-dir",
                "file": UploadFile(filename="uploaded.txt", file=BytesIO(b"payload")),
            }

    upload_response = asyncio.run(file_browser_api.api_files_upload(_UploadRequest()))
    assert upload_response.status_code == 400
    assert not (outside_dir / "uploaded.txt").exists()


def test_root_delete_is_rejected_and_image_url_is_encoded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
):
    root = tmp_path / "root"
    root.mkdir()
    image_path = root / "hello world.png"
    image_path.write_bytes(b"png")

    with _make_client(root, monkeypatch) as client:
        delete_response = client.post("/api/files/delete", json={"path": "."})
        assert delete_response.status_code == 400
        assert "configured root directory" in delete_response.json()["error"].lower()

        read_response = client.get("/api/files/read?path=hello%20world.png")
        assert read_response.status_code == 200
        assert "hello%20world.png" in read_response.json()["content_url"]


def test_upload_limit_returns_413(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("OUROBOROS_FILE_BROWSER_DEFAULT", str(root))
    monkeypatch.setattr(file_browser_api, "_FILE_BROWSER_MAX_UPLOAD_BYTES", 4)

    class _Client:
        host = "testclient"

    class _Request:
        client = _Client()

        async def form(self):
            return {
                "path": ".",
                "file": UploadFile(filename="big.txt", file=BytesIO(b"12345")),
            }

    response = asyncio.run(file_browser_api.api_files_upload(_Request()))

    assert response.status_code == 413
    payload = json.loads(response.body.decode("utf-8"))
    assert "upload exceeds" in payload["error"].lower()
