import importlib
import json
import os
import pathlib
import subprocess
import sys
import types

import ouroboros.launcher_bootstrap as bootstrap_module


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD_REPO_BUNDLE = REPO_ROOT / "scripts" / "build_repo_bundle.py"


# These keys must be scrubbed from every ``build_repo_bundle.py``
# subprocess: GitHub Actions tag-push runs set GITHUB_REF_* globally,
# which otherwise bleeds into temp-repo subprocesses and confuses
# ``_resolve_release_tag``.
_BUILD_BUNDLE_ENV_SCRUB_KEYS = (
    "OUROBOROS_RELEASE_TAG",
    "GITHUB_REF",
    "GITHUB_REF_TYPE",
    "GITHUB_REF_NAME",
)


def _scrubbed_env() -> "dict[str, str]":
    env = dict(os.environ)
    for key in _BUILD_BUNDLE_ENV_SCRUB_KEYS:
        env.pop(key, None)
    return env


def _reload_bootstrap():
    return importlib.reload(bootstrap_module)


def _log_stub():
    return types.SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )


def _make_context(bundle_dir, repo_dir):
    return bootstrap_module.BootstrapContext(
        bundle_dir=bundle_dir,
        repo_dir=repo_dir,
        data_dir=repo_dir.parent / "data",
        settings_path=repo_dir.parent / "settings.json",
        embedded_python=sys.executable,
        app_version="4.50.0-rc.2",
        hidden_run=subprocess.run,
        save_settings=lambda settings: None,
        log=_log_stub(),
    )


def _run(cmd, *, cwd):
    subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True, text=True)


def _git_output(cwd, *args):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _make_bundle_source(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _run(["git", "init"], cwd=src)
    _run(["git", "config", "user.name", "Test"], cwd=src)
    _run(["git", "config", "user.email", "test@example.com"], cwd=src)
    _run(["git", "checkout", "-b", "ouroboros"], cwd=src)
    _run(["git", "remote", "add", "origin", "https://github.com/joi-lab/ouroboros-desktop.git"], cwd=src)
    (src / "VERSION").write_text("4.50.0-rc.2\n", encoding="utf-8")
    (src / "server.py").write_text("print('bundle-v1')\n", encoding="utf-8")
    _run(["git", "add", "VERSION", "server.py"], cwd=src)
    _run(["git", "commit", "-m", "bundle v1"], cwd=src)
    _run(["git", "tag", "-a", "v4.50.0-rc.2", "-m", "Release v4.50.0-rc.2"], cwd=src)
    return src


def _write_bundle(repo_src, bundle_dir):
    bundle_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            str(BUILD_REPO_BUNDLE),
            "--repo-root",
            str(repo_src),
            "--output-bundle",
            str(bundle_dir / "repo.bundle"),
            "--output-manifest",
            str(bundle_dir / "repo_bundle_manifest.json"),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=_scrubbed_env(),
    )


def test_ensure_managed_repo_clones_from_embedded_bundle(tmp_path):
    bootstrap = _reload_bootstrap()
    src = _make_bundle_source(tmp_path)
    bundle_dir = tmp_path / "bundle"
    repo_dir = tmp_path / "repo"
    _write_bundle(src, bundle_dir)

    ctx = _make_context(bundle_dir, repo_dir)
    outcome = bootstrap.ensure_managed_repo(ctx)

    assert outcome == "created"
    assert (repo_dir / ".git").is_dir()
    assert (repo_dir / "server.py").read_text(encoding="utf-8") == "print('bundle-v1')\n"
    assert _git_output(repo_dir, "branch", "--show-current") == "ouroboros"
    meta = bootstrap.load_repo_manifest(repo_dir)
    assert meta["managed_remote_branch"] == "ouroboros"
    assert meta["managed_local_branch"] == "ouroboros"
    assert meta["release_tag"] == "v4.50.0-rc.2"
    assert meta["bundle_sha256"]
    assert (repo_dir / ".git" / bootstrap.BOOTSTRAP_PIN_MARKER_NAME).exists()


def test_ensure_managed_repo_preserves_origin_on_unchanged_boot(tmp_path):
    bootstrap = _reload_bootstrap()
    src = _make_bundle_source(tmp_path)
    bundle_dir = tmp_path / "bundle"
    repo_dir = tmp_path / "repo"
    _write_bundle(src, bundle_dir)

    ctx = _make_context(bundle_dir, repo_dir)
    assert bootstrap.ensure_managed_repo(ctx) == "created"

    _run(["git", "remote", "add", "origin", "https://github.com/example/fork.git"], cwd=repo_dir)

    outcome = bootstrap.ensure_managed_repo(ctx)

    assert outcome == "unchanged"
    remotes = set(_git_output(repo_dir, "remote").splitlines())
    assert remotes == {"managed", "origin"}


def test_sync_existing_repo_from_bundle_replaces_legacy_snapshot(tmp_path):
    bootstrap = _reload_bootstrap()
    src = _make_bundle_source(tmp_path)
    bundle_dir = tmp_path / "bundle"
    repo_dir = tmp_path / "repo"
    _write_bundle(src, bundle_dir)

    repo_dir.mkdir()
    (repo_dir / "server.py").write_text("print('legacy-snapshot')\n", encoding="utf-8")

    ctx = _make_context(bundle_dir, repo_dir)
    bootstrap.sync_existing_repo_from_bundle(ctx)

    assert (repo_dir / ".git").is_dir()
    assert (repo_dir / "server.py").read_text(encoding="utf-8") == "print('bundle-v1')\n"
    archived = list((ctx.data_dir / "archive" / "managed_repo").iterdir())
    assert archived
    assert (archived[0] / "server.py").read_text(encoding="utf-8") == "print('legacy-snapshot')\n"


def test_ensure_managed_repo_preserves_checkout_when_embedded_bundle_changes(tmp_path):
    bootstrap = _reload_bootstrap()
    src = _make_bundle_source(tmp_path)
    bundle_dir = tmp_path / "bundle"
    repo_dir = tmp_path / "repo"
    _write_bundle(src, bundle_dir)

    ctx = _make_context(bundle_dir, repo_dir)
    assert bootstrap.ensure_managed_repo(ctx) == "created"
    _run(["git", "remote", "add", "origin", "https://github.com/example/fork.git"], cwd=repo_dir)
    (repo_dir / "server.py").write_text("print('local-self-modification')\n", encoding="utf-8")
    _run(["git", "add", "server.py"], cwd=repo_dir)
    _run(["git", "commit", "-m", "local self modification"], cwd=repo_dir)
    local_head = _git_output(repo_dir, "rev-parse", "HEAD")

    (src / "server.py").write_text("print('bundle-v2')\n", encoding="utf-8")
    _run(["git", "add", "server.py"], cwd=src)
    _run(["git", "commit", "-m", "bundle v2"], cwd=src)
    # Re-point the annotated release tag onto the new HEAD so the bundle
    # builder's HEAD-tag check still passes (the test is exercising the
    # bundle-replacement path, not a VERSION bump).
    _run(["git", "tag", "-d", "v4.50.0-rc.2"], cwd=src)
    _run(["git", "tag", "-a", "v4.50.0-rc.2", "-m", "Release v4.50.0-rc.2 (v2)"], cwd=src)
    _write_bundle(src, bundle_dir)

    outcome = bootstrap.ensure_managed_repo(ctx)

    assert outcome == "metadata-updated"
    assert _git_output(repo_dir, "rev-parse", "HEAD") == local_head
    assert (repo_dir / "server.py").read_text(encoding="utf-8") == "print('local-self-modification')\n"
    assert set(_git_output(repo_dir, "remote").splitlines()) == {"managed", "origin"}
    assert bootstrap.load_repo_manifest(repo_dir)["source_sha"] == _git_output(src, "rev-parse", "HEAD")
    assert not (ctx.data_dir / "archive" / "managed_repo").exists()


def test_load_bundle_manifest_rejects_app_version_mismatch(tmp_path):
    bootstrap = _reload_bootstrap()
    src = _make_bundle_source(tmp_path)
    bundle_dir = tmp_path / "bundle"
    repo_dir = tmp_path / "repo"
    _write_bundle(src, bundle_dir)

    ctx = bootstrap.BootstrapContext(
        bundle_dir=bundle_dir,
        repo_dir=repo_dir,
        data_dir=repo_dir.parent / "data",
        settings_path=repo_dir.parent / "settings.json",
        embedded_python=sys.executable,
        app_version="4.50.0-rc.3",
        hidden_run=subprocess.run,
        save_settings=lambda settings: None,
        log=_log_stub(),
    )

    try:
        bootstrap.load_bundle_manifest(ctx)
        assert False, "Expected app_version mismatch to raise"
    except RuntimeError as exc:
        assert "app_version" in str(exc)


def test_load_bundle_manifest_rejects_release_tag_mismatch(tmp_path):
    bootstrap = _reload_bootstrap()
    src = _make_bundle_source(tmp_path)
    bundle_dir = tmp_path / "bundle"
    repo_dir = tmp_path / "repo"
    _write_bundle(src, bundle_dir)

    manifest_path = bundle_dir / "repo_bundle_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["release_tag"] = "v4.50.0-rc.3"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    ctx = _make_context(bundle_dir, repo_dir)

    try:
        bootstrap.load_bundle_manifest(ctx)
        assert False, "Expected release_tag mismatch to raise"
    except RuntimeError as exc:
        assert "release_tag" in str(exc)


def test_ensure_managed_repo_rejects_tampered_bundle(tmp_path):
    bootstrap = _reload_bootstrap()
    src = _make_bundle_source(tmp_path)
    bundle_dir = tmp_path / "bundle"
    repo_dir = tmp_path / "repo"
    _write_bundle(src, bundle_dir)
    (bundle_dir / "repo.bundle").write_bytes(b"tampered-bundle")

    ctx = _make_context(bundle_dir, repo_dir)

    try:
        bootstrap.ensure_managed_repo(ctx)
        assert False, "Expected bundle hash mismatch to raise"
    except RuntimeError as exc:
        assert "bundle hash mismatch" in str(exc)


# ---------------------------------------------------------------------------
# Host service port cleanup (formerly test_launcher_host_service_cleanup.py)
# ---------------------------------------------------------------------------


def test_host_service_cleanup_uses_configured_port(monkeypatch):
    import launcher

    killed: list[int] = []

    monkeypatch.setenv("OUROBOROS_HOST_SERVICE_PORT", "9876")
    monkeypatch.setattr(launcher, "_kill_stale_on_port", lambda port: killed.append(port))

    launcher._kill_stale_runtime_ports(8765)

    assert killed == [8765, 9876]


def test_agent_lifecycle_preflight_cleans_host_service_port(monkeypatch):
    import launcher

    killed: list[int] = []

    class FakeProcess:
        returncode = 0

        def wait(self):
            launcher._shutdown_event.set()

    launcher._shutdown_event.clear()
    monkeypatch.setattr(launcher, "_host_service_port", lambda: 9876)
    monkeypatch.setattr(launcher, "_kill_stale_on_port", lambda port: killed.append(port))
    monkeypatch.setattr(launcher, "start_agent", lambda port: FakeProcess())
    monkeypatch.setattr(launcher, "_poll_port_file", lambda timeout=30: 8765)
    monkeypatch.setattr(launcher, "_wait_for_server", lambda port, timeout=30.0: True)
    monkeypatch.setattr(launcher, "_agent_job", None)
    monkeypatch.setattr(launcher, "log", types.SimpleNamespace(info=lambda *args, **kwargs: None))

    try:
        launcher.agent_lifecycle_loop(port=8765)
    finally:
        launcher._shutdown_event.clear()

    assert killed[:2] == [8765, 9876]
