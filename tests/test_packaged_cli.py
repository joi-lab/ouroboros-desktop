from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

import pytest


def _make_bundle_root(tmp_path: pathlib.Path, root: pathlib.Path | None = None) -> pathlib.Path:
    root = root or tmp_path / "Ouroboros"
    (root / "python-standalone" / "bin").mkdir(parents=True)
    (root / "python-standalone" / "bin" / "python3").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "python-standalone" / "python.exe").write_text("@echo off\r\n", encoding="utf-8")
    (root / "repo.bundle").write_text("bundle", encoding="utf-8")
    (root / "repo_bundle_manifest.json").write_text("{}", encoding="utf-8")
    (root / "VERSION").write_text("5.29.0-rc.2\n", encoding="utf-8")
    (root / "bin").mkdir()
    (root / "bin" / "ouroboros").write_text("# Ouroboros packaged CLI shim\n", encoding="utf-8")
    return root


def test_packaged_cli_resolves_bundle_from_nested_bin(tmp_path):
    from ouroboros import packaged_cli

    root = _make_bundle_root(tmp_path)

    assert packaged_cli._find_bundle_root([root / "bin" / "ouroboros"]) == root


def test_packaged_cli_run_start_launches_desktop_and_strips_start(tmp_path, monkeypatch):
    from ouroboros import packaged_cli

    root = _make_bundle_root(tmp_path)
    launched = []
    inner = {}

    monkeypatch.setenv("OUROBOROS_PACKAGED_BUNDLE_ROOT", str(root))
    monkeypatch.setenv("OUROBOROS_CLI_START_TIMEOUT", "1")
    monkeypatch.setattr(packaged_cli, "_bootstrap_runtime", lambda runtime: None)
    monkeypatch.setattr(packaged_cli, "_gateway_supervisor_ready", lambda _url: False)
    monkeypatch.setattr(packaged_cli, "_wait_for_ready", lambda _url, _data_dir, explicit_url: "http://127.0.0.1:8765")
    monkeypatch.setattr(packaged_cli, "_launch_desktop_app", lambda runtime: launched.append(runtime.bundle_root))

    def fake_inner(runtime, args):
        inner["args"] = list(args)
        return 0

    monkeypatch.setattr(packaged_cli, "_run_inner_cli", fake_inner)

    assert packaged_cli.main(["run", "--start", "2+2?"]) == 0
    assert launched == [root]
    assert inner["args"] == ["run", "2+2?"]


def test_packaged_cli_does_not_treat_prompt_start_text_as_option(tmp_path, monkeypatch):
    from ouroboros import packaged_cli

    root = _make_bundle_root(tmp_path)
    launched = []
    inner = {}

    monkeypatch.setenv("OUROBOROS_PACKAGED_BUNDLE_ROOT", str(root))
    monkeypatch.setattr(packaged_cli, "_bootstrap_runtime", lambda runtime: None)
    monkeypatch.setattr(packaged_cli, "_launch_desktop_app", lambda runtime: launched.append(runtime.bundle_root))
    def fake_inner(_runtime, args):
        inner["args"] = list(args)
        return 0

    monkeypatch.setattr(packaged_cli, "_run_inner_cli", fake_inner)

    assert packaged_cli.main(["run", "hello", "--start"]) == 0
    assert launched == []
    assert inner["args"] == ["run", "hello", "--start"]


def test_packaged_cli_does_not_intercept_abbreviated_start(tmp_path, monkeypatch):
    from ouroboros import packaged_cli

    root = _make_bundle_root(tmp_path)
    launched = []
    inner = {}

    monkeypatch.setenv("OUROBOROS_PACKAGED_BUNDLE_ROOT", str(root))
    monkeypatch.setattr(packaged_cli, "_bootstrap_runtime", lambda runtime: None)
    monkeypatch.setattr(packaged_cli, "_launch_desktop_app", lambda runtime: launched.append(runtime.bundle_root))
    def fake_inner(_runtime, args):
        inner["args"] = list(args)
        return 0

    monkeypatch.setattr(packaged_cli, "_run_inner_cli", fake_inner)

    assert packaged_cli.main(["run", "--sta", "2+2?"]) == 0
    assert launched == []
    assert inner["args"] == ["run", "--sta", "2+2?"]


def test_packaged_cli_rejects_packaged_server_subcommand(tmp_path, monkeypatch, capsys):
    from ouroboros import packaged_cli

    root = _make_bundle_root(tmp_path)
    monkeypatch.setenv("OUROBOROS_PACKAGED_BUNDLE_ROOT", str(root))

    assert packaged_cli.main(["server"]) == 2
    assert "packaged 'ouroboros server' is not supported" in capsys.readouterr().err


def test_packaged_cli_inner_env_ignores_inherited_repo_and_data(tmp_path, monkeypatch):
    from ouroboros.packaged_cli import PackagedRuntime, _inner_cli_env

    runtime = PackagedRuntime(
        bundle_root=tmp_path / "bundle",
        embedded_python=tmp_path / "bundle" / "python-standalone" / "bin" / "python3",
        app_root=tmp_path / "home" / "Ouroboros",
        repo_dir=tmp_path / "home" / "Ouroboros" / "repo",
        data_dir=tmp_path / "home" / "Ouroboros" / "data",
        app_version="5.29.0-rc.2",
    )
    monkeypatch.setenv("PYTHONPATH", "/bad")
    monkeypatch.setenv("OUROBOROS_REPO_DIR", "/bad/repo")
    monkeypatch.setenv("OUROBOROS_DATA_DIR", "/bad/data")
    monkeypatch.setenv("OUROBOROS_URL", "http://127.0.0.1:9000")

    env = _inner_cli_env(runtime)

    assert env["PYTHONPATH"] == str(runtime.repo_dir)
    assert env["OUROBOROS_REPO_DIR"] == str(runtime.repo_dir)
    assert env["OUROBOROS_DATA_DIR"] == str(runtime.data_dir)
    assert env["OUROBOROS_URL"] == "http://127.0.0.1:9000"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["PYTHONPYCACHEPREFIX"] == str(runtime.data_dir / "state" / "pycache")


def test_installer_plan_chooses_user_local_path_dir(tmp_path, monkeypatch):
    from ouroboros.packaged_cli_install import plan_posix_install

    root = _make_bundle_root(tmp_path)
    home = tmp_path / "home"
    target_dir = home / ".local" / "bin"
    target_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(target_dir))

    plan = plan_posix_install(root)

    assert plan.target == target_dir / "ouroboros"
    assert plan.source == root / "bin" / "ouroboros"


def test_installer_plan_accepts_expected_wrapper_in_sibling_resources_dir(tmp_path, monkeypatch):
    from ouroboros.packaged_cli_install import plan_posix_install

    contents = tmp_path / "Ouroboros.app" / "Contents"
    root = _make_bundle_root(tmp_path, root=contents / "Frameworks")
    (root / "bin" / "ouroboros").unlink()
    resources_bin = contents / "Resources" / "bin"
    resources_bin.mkdir(parents=True)
    wrapper = resources_bin / "ouroboros"
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
    target_dir = tmp_path / "target-bin"
    target_dir.mkdir()
    monkeypatch.setenv("OUROBOROS_PACKAGED_CLI_WRAPPER", str(wrapper))

    plan = plan_posix_install(root, target_dir=target_dir)

    assert plan.source == wrapper
    assert plan.target == target_dir / "ouroboros"


def test_installer_rejects_wrapper_source_outside_bundle(tmp_path, monkeypatch):
    from ouroboros.packaged_cli import PackagedCLIError
    from ouroboros.packaged_cli_install import plan_posix_install

    root = _make_bundle_root(tmp_path)
    outside = tmp_path / "other" / "bin" / "ouroboros"
    outside.parent.mkdir(parents=True)
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    target_dir = tmp_path / "target-bin"
    target_dir.mkdir()
    monkeypatch.setenv("OUROBOROS_PACKAGED_CLI_WRAPPER", str(outside))

    with pytest.raises(PackagedCLIError, match="outside this bundle"):
        plan_posix_install(root, target_dir=target_dir)


def test_installer_refuses_unrelated_existing_command(tmp_path):
    from ouroboros.packaged_cli import PackagedCLIError
    from ouroboros.packaged_cli_install import plan_posix_install

    root = _make_bundle_root(tmp_path)
    target_dir = tmp_path / "bin"
    target_dir.mkdir()
    (target_dir / "ouroboros").write_text("#!/bin/sh\necho nope\n", encoding="utf-8")

    with pytest.raises(PackagedCLIError, match="refusing to overwrite existing non-Ouroboros command"):
        plan_posix_install(root, target_dir=target_dir)


def test_installer_rejects_macos_dmg_or_translocation_paths():
    from ouroboros.packaged_cli import PackagedCLIError
    from ouroboros.packaged_cli_install import reject_unstable_macos_path

    with pytest.raises(PackagedCLIError, match="refusing to install CLI from a DMG"):
        reject_unstable_macos_path(pathlib.Path("/Volumes/Ouroboros/Ouroboros.app/Contents/Resources"))
    with pytest.raises(PackagedCLIError, match="refusing to install CLI from a DMG"):
        reject_unstable_macos_path(pathlib.Path("/private/var/AppTranslocation/Ouroboros.app/Contents/Resources"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell wrapper test")
def test_posix_wrapper_ignores_poisoned_env_and_finds_internal_root(tmp_path, monkeypatch):
    app = tmp_path / "Ouroboros"
    bin_dir = app / "bin"
    internal = app / "_internal"
    python_dir = internal / "python-standalone" / "bin"
    bin_dir.mkdir(parents=True)
    python_dir.mkdir(parents=True)
    shutil.copyfile(pathlib.Path("packaging/cli/ouroboros"), bin_dir / "ouroboros")
    os.chmod(bin_dir / "ouroboros", 0o755)
    (internal / "repo.bundle").write_text("bundle", encoding="utf-8")
    (internal / "repo_bundle_manifest.json").write_text("{}", encoding="utf-8")
    log = tmp_path / "python.log"
    (python_dir / "python3").write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$OUROBOROS_PACKAGED_BUNDLE_ROOT\" > {log}\n"
        f"printf '%s\\n' \"$*\" >> {log}\n"
        f"printf '%s\\n' \"$PYTHONDONTWRITEBYTECODE\" >> {log}\n"
        f"printf '%s\\n' \"$PYTHONPYCACHEPREFIX\" >> {log}\n",
        encoding="utf-8",
    )
    os.chmod(python_dir / "python3", 0o755)
    poisoned = tmp_path / "poison"
    (poisoned / "python-standalone" / "bin").mkdir(parents=True)
    (poisoned / "repo.bundle").write_text("bad", encoding="utf-8")
    (poisoned / "repo_bundle_manifest.json").write_text("{}", encoding="utf-8")
    (poisoned / "python-standalone" / "bin" / "python3").write_text("exit 9\n", encoding="utf-8")
    os.chmod(poisoned / "python-standalone" / "bin" / "python3", 0o755)
    monkeypatch.setenv("OUROBOROS_PACKAGED_BUNDLE_ROOT", str(poisoned))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    subprocess.run([str(bin_dir / "ouroboros"), "status"], cwd=tmp_path, check=True)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[0] == str(internal)
    assert lines[1] == "-m ouroboros.packaged_cli status"
    assert lines[2] == "1"
    assert lines[3] == str(tmp_path / "cache" / "ouroboros" / "pycache")


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell wrapper test")
def test_posix_wrapper_imports_packaged_cli_from_internal_bundle(tmp_path, monkeypatch):
    app = tmp_path / "Ouroboros"
    bin_dir = app / "bin"
    internal = app / "_internal"
    python_dir = internal / "python-standalone" / "bin"
    module_dir = internal / "ouroboros"
    bin_dir.mkdir(parents=True)
    python_dir.mkdir(parents=True)
    module_dir.mkdir(parents=True)
    shutil.copyfile(pathlib.Path("packaging/cli/ouroboros"), bin_dir / "ouroboros")
    os.chmod(bin_dir / "ouroboros", 0o755)
    (internal / "repo.bundle").write_text("bundle", encoding="utf-8")
    (internal / "repo_bundle_manifest.json").write_text("{}", encoding="utf-8")
    os.symlink(sys.executable, python_dir / "python3")
    marker = tmp_path / "marker.txt"
    (module_dir / "__init__.py").write_text("", encoding="utf-8")
    (module_dir / "packaged_cli.py").write_text(
        "import os, pathlib, sys\n"
        "pathlib.Path(os.environ['OUROBOROS_TEST_MARKER']).write_text('\\n'.join([\n"
        "    os.environ.get('PYTHONPATH', ''),\n"
        "    os.environ.get('OUROBOROS_PACKAGED_BUNDLE_ROOT', ''),\n"
        "    ' '.join(sys.argv[1:]),\n"
        "]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OUROBOROS_TEST_MARKER", str(marker))

    subprocess.run([str(bin_dir / "ouroboros"), "status"], cwd=tmp_path, check=True)

    lines = marker.read_text(encoding="utf-8").splitlines()
    assert lines == [str(internal), str(internal), "status"]


def test_packaged_cli_install_delegates_windows_path_mutation_to_platform_layer():
    source = pathlib.Path("ouroboros/packaged_cli_install.py").read_text(encoding="utf-8")

    assert "ensure_windows_user_path" in source
    assert "import winreg" not in source
    assert "ctypes.windll" not in source


def test_packaged_cli_install_delegates_macos_path_check_to_platform_layer():
    source = pathlib.Path("ouroboros/packaged_cli_install.py").read_text(encoding="utf-8")

    assert "is_unstable_macos_app_path" in source
    assert 'startswith("/Volumes/")' not in source
    assert '"AppTranslocation" in' not in source
