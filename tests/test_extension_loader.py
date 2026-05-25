"""Phase 4 regression tests for ``ouroboros.extension_loader``.

Covers PluginAPI surface: register_tool / register_route /
register_ws_handler / register_ui_tab + permission gating +
namespace enforcement + unload cleanup.
"""
from __future__ import annotations

import json
import pathlib
import re
from typing import Any, Dict

import pytest

from ouroboros import extension_loader
from ouroboros.contracts.plugin_api import (
    ExtensionRegistrationError,
    FORBIDDEN_EXTENSION_SETTINGS,
    PluginAPI,
    VALID_EXTENSION_PERMISSIONS,
)
from ouroboros.skill_loader import (
    SkillReviewState,
    compute_content_hash,
    find_skill,
    save_enabled,
    save_review_state,
)


from tests._shared import clean_extension_runtime_state


@pytest.fixture(autouse=True)
def _clear_loader_state(monkeypatch):
    """Reset the module-level registries between tests."""
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    clean_extension_runtime_state()
    yield
    clean_extension_runtime_state()


def _write_ext_skill(
    repo_root: pathlib.Path,
    name: str,
    *,
    plugin_body: str,
    permissions: list[str],
    env_from_settings: list[str] | None = None,
    entry: str = "plugin.py",
    extra_frontmatter: str = "",
) -> pathlib.Path:
    skill_dir = repo_root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    perms_yaml = json.dumps(permissions)
    env_yaml = json.dumps(env_from_settings or [])
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            f"name: {name}\n"
            "description: Phase 4 extension.\n"
            "version: 0.1.0\n"
            "type: extension\n"
            f"entry: {entry}\n"
            f"permissions: {perms_yaml}\n"
            f"env_from_settings: {env_yaml}\n"
            f"{extra_frontmatter}"
            "---\n"
            "body\n"
        ),
        encoding="utf-8",
    )
    entry_path = skill_dir / entry
    entry_path.parent.mkdir(parents=True, exist_ok=True)
    entry_path.write_text(plugin_body, encoding="utf-8")
    return skill_dir


def _prepare_extension(
    tmp_path: pathlib.Path,
    name: str,
    plugin_body: str,
    permissions: list[str],
    env_from_settings: list[str] | None = None,
    extra_frontmatter: str = "",
):
    """Write + enable + PASS-review an extension so the loader accepts it."""
    from ouroboros.skill_loader import find_skill
    repo_root = tmp_path / "skills"
    drive_root = tmp_path / "drive"
    drive_root.mkdir(exist_ok=True)
    skill_dir = _write_ext_skill(
        repo_root,
        name,
        plugin_body=plugin_body,
        permissions=permissions,
        env_from_settings=env_from_settings,
        extra_frontmatter=extra_frontmatter,
    )
    loaded = find_skill(drive_root, name, repo_path=str(repo_root))
    assert loaded is not None
    save_enabled(drive_root, name, True)
    save_review_state(
        drive_root,
        name,
        SkillReviewState(status="pass", content_hash=loaded.content_hash),
    )
    # Refetch with fresh state on the loaded struct.
    loaded = find_skill(drive_root, name, repo_path=str(repo_root))
    assert loaded is not None
    return loaded, repo_root, drive_root


def _mark_isolated_deps_installed(drive_root: pathlib.Path, loaded) -> None:
    from ouroboros.marketplace.install_specs import install_specs_hash
    from ouroboros.marketplace.isolated_deps import FINGERPRINT_FILENAME, isolated_env_dir
    from ouroboros.skill_dependencies import auto_install_specs_for_skill
    from ouroboros.skill_loader import skill_state_dir

    auto_specs = auto_install_specs_for_skill(drive_root, loaded)
    assert auto_specs
    payload = {
        "status": "installed",
        "specs_hash": install_specs_hash(auto_specs),
        "installed": auto_specs,
    }
    state_dir = skill_state_dir(drive_root, loaded.name)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "deps.json").write_text(json.dumps(payload), encoding="utf-8")
    env_dir = isolated_env_dir(loaded.skill_dir)
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / FINGERPRINT_FILENAME).write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# PluginAPI contract shape
# ---------------------------------------------------------------------------


def test_plugin_api_impl_matches_protocol():
    """Runtime-checkable Protocol must structurally accept PluginAPIImpl."""
    impl = extension_loader.PluginAPIImpl(
        skill_name="x",
        permissions=(),
        env_allowlist=(),
        state_dir=pathlib.Path("/tmp"),
        settings_reader=lambda: {},
    )
    assert isinstance(impl, PluginAPI)
    info = impl.get_runtime_info()
    assert info["app_version"]
    assert sorted(info) == [
        "app_version",
        "data_dir",
        "runtime_mode",
        "server_port",
        "skill_dir",
        "state_dir",
    ]


def test_plugin_api_runtime_info_uses_port_file(tmp_path, monkeypatch):
    """server_port must reflect the actual bound server port written by
    server.py/launcher, not the static AGENT_SERVER_PORT fallback."""
    from ouroboros import config as cfg

    port_file = tmp_path / "state" / "server_port"
    port_file.parent.mkdir()
    port_file.write_text("9012\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "PORT_FILE", port_file)
    impl = extension_loader.PluginAPIImpl(
        skill_name="x",
        permissions=(),
        env_allowlist=(),
        state_dir=tmp_path / "state",
        settings_reader=lambda: {},
    )

    assert impl.get_runtime_info()["server_port"] == 9012


def test_register_settings_section_lifecycle(tmp_path):
    loaded, _repo_root, drive_root = _prepare_extension(
        tmp_path,
        "settings_ext",
        plugin_body=(
            "def register(api):\n"
            "    api.register_settings_section('config', 'Config', schema={'components': [\n"
            "        {'type': 'markdown', 'text': 'hello'}\n"
            "    ]})\n"
        ),
        permissions=["widget"],
    )

    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    sections = extension_loader.snapshot()["settings_sections"]
    assert len(sections) == 1
    assert sections[0]["skill"] == "settings_ext"
    assert sections[0]["section_id"] == "config"

    extension_loader.unload_extension("settings_ext")
    assert extension_loader.snapshot()["settings_sections"] == []


def test_forbidden_extension_settings_carries_repo_secrets():
    """The forbidden-settings tuple must match the repo-credentials set
    ``skill_exec`` already refuses to forward."""
    assert "OPENROUTER_API_KEY" in FORBIDDEN_EXTENSION_SETTINGS
    assert "GITHUB_TOKEN" in FORBIDDEN_EXTENSION_SETTINGS
    assert "OUROBOROS_NETWORK_PASSWORD" in FORBIDDEN_EXTENSION_SETTINGS


def test_valid_permissions_is_closed_set():
    for needed in ("tool", "route", "ws_handler", "widget", "read_settings", "net", "fs", "subprocess"):
        assert needed in VALID_EXTENSION_PERMISSIONS


# ---------------------------------------------------------------------------
# Successful load + registration
# ---------------------------------------------------------------------------


def test_load_extension_registers_tool(tmp_path):
    plugin = (
        "def _echo(ctx, message='hi'):\n"
        "    return f'echo: {message}'\n"
        "def register(api):\n"
        "    api.register_tool(\n"
        "        'echo',\n"
        "        _echo,\n"
        "        description='echo',\n"
        "        schema={'type': 'object', 'properties': {'message': {'type': 'string'}}},\n"
        "    )\n"
    )
    loaded, _, drive_root = _prepare_extension(tmp_path, "ext1", plugin, permissions=["tool"])
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    tool_name = extension_loader.extension_surface_name("ext1", "echo")
    tool = extension_loader.get_tool(tool_name)
    assert tool is not None
    assert tool["name"] == tool_name
    assert callable(tool["handler"])


def test_extension_surface_names_are_provider_safe_without_renaming_skill_identity():
    from ouroboros.skill_loader import _sanitize_skill_name

    dotted = "foo.bar"
    unicode_name = "погода"
    dotted_tool = extension_loader.extension_surface_name(dotted, "fetch")
    unicode_tool = extension_loader.extension_surface_name(unicode_name, "fetch")
    generated_token_twin = "foo_bar_336d1b3d72"

    assert _sanitize_skill_name(dotted) == dotted
    assert _sanitize_skill_name("foo_bar") == "foo_bar"
    assert dotted_tool != extension_loader.extension_surface_name("foo_bar", "fetch")
    assert dotted_tool != extension_loader.extension_surface_name(generated_token_twin, "fetch")
    assert extension_loader.extension_surface_name("foo", "bar_baz") != extension_loader.extension_surface_name("foo_bar", "baz")
    for tool_name in (dotted_tool, unicode_tool):
        assert re.match(r"^[A-Za-z0-9_-]{1,64}$", tool_name)
        assert "." not in tool_name
        assert extension_loader.parse_extension_surface_name(tool_name) is not None


def test_load_extension_rejects_outward_symlink_in_skill_tree(tmp_path):
    import os, platform

    if platform.system() == "Windows":
        pytest.skip("symlink creation requires admin on Windows")
    skill_dir = tmp_path / "skills" / "symlinked"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            "name: symlinked\n"
            "description: Symlink escape regression.\n"
            "version: 0.1.0\n"
            "type: extension\n"
            "entry: plugin.py\n"
            "permissions: [\"tool\"]\n"
            "env_from_settings: []\n"
            "---\n"
            "body\n"
        ),
        encoding="utf-8",
    )
    (skill_dir / "plugin.py").write_text(
        (
            "from .helper import SECRET\n"
            "def _echo(ctx):\n"
            "    return SECRET\n"
            "def register(api):\n"
            "    api.register_tool('echo', _echo, description='echo', schema={})\n"
        ),
        encoding="utf-8",
    )
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    save_enabled(drive_root, "symlinked", True)
    loaded = find_skill(drive_root, "symlinked", repo_path=str(skill_dir.parent))
    assert loaded is not None
    save_review_state(
        drive_root,
        "symlinked",
        SkillReviewState(status="pass", content_hash=loaded.content_hash),
    )
    outside = tmp_path / "outside_helper.py"
    outside.write_text("SECRET = 'escape'\n", encoding="utf-8")
    os.symlink(outside, skill_dir / "helper.py")

    loaded = find_skill(drive_root, "symlinked", repo_path=str(skill_dir.parent))
    assert loaded is not None
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is not None
    assert "symlink" in err.lower()
    assert extension_loader.get_tool(extension_loader.extension_surface_name("symlinked", "echo")) is None


def test_load_extension_ignores_isolated_env_symlinks_when_staging(tmp_path):
    import os
    import platform

    if platform.system() == "Windows":
        pytest.skip("symlink creation requires admin on Windows")
    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "env_symlink",
        (
            "def register(api):\n"
            "    api.register_tool('ok', lambda ctx: 'ok', description='ok', schema={})\n"
        ),
        permissions=["tool"],
    )
    skill_dir = repo_root / "env_symlink"
    bin_dir = skill_dir / ".ouroboros_env" / "python" / "bin"
    bin_dir.mkdir(parents=True)
    os.symlink("/usr/bin/env", bin_dir / "python")

    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)

    assert err is None, err
    with extension_loader._lock:
        import_root = pathlib.Path(extension_loader._extensions["env_symlink"].import_root)
    assert not (import_root / "skill" / ".ouroboros_env").exists()
    extension_loader.unload_extension("env_symlink")


def test_load_extension_does_not_import_untracked_isolated_python_deps(tmp_path):
    import sys

    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "env_untracked",
        (
            "import dummy_untracked_pkg\n"
            "def register(api):\n"
            "    api.register_tool('value', lambda ctx: dummy_untracked_pkg.VALUE, description='value', schema={})\n"
        ),
        permissions=["tool"],
    )
    skill_dir = repo_root / "env_untracked"
    pkg_dir = (
        skill_dir
        / ".ouroboros_env"
        / "python"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
        / "dummy_untracked_pkg"
    )
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("VALUE = 'untracked'\n", encoding="utf-8")

    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)

    assert err is not None
    assert "dummy_untracked_pkg" in err


def test_load_extension_imports_and_unloads_isolated_python_deps(tmp_path):
    import importlib
    import sys

    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "env_import",
        (
            "import dummy_pkg\n"
            "def register(api):\n"
            "    api.register_tool('value', lambda ctx: dummy_pkg.VALUE, description='value', schema={})\n"
        ),
        permissions=["tool"],
        extra_frontmatter="dependencies:\n  - dummy_pkg\n",
    )
    skill_dir = repo_root / "env_import"
    site_dir = (
        skill_dir
        / ".ouroboros_env"
        / "python"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    pkg_dir = site_dir / "dummy_pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("VALUE = 'from-isolated-env'\n", encoding="utf-8")
    _mark_isolated_deps_installed(drive_root, loaded)

    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)

    assert err is None, err
    tool = extension_loader.get_tool(extension_loader.extension_surface_name("env_import", "value"))
    assert tool is not None
    assert tool["handler"](None) == "from-isolated-env"
    assert str(site_dir.resolve()) not in sys.path

    extension_loader.unload_extension("env_import")

    assert str(site_dir.resolve()) not in sys.path
    assert "dummy_pkg" not in sys.modules
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("dummy_pkg")


def test_load_extension_does_not_execute_isolated_deps_pth_files(tmp_path):
    import sys

    marker = tmp_path / "pth_executed.txt"
    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "env_pth",
        (
            "import dummy_pth_pkg\n"
            "def register(api):\n"
            "    api.register_tool('value', lambda ctx: dummy_pth_pkg.VALUE, description='value', schema={})\n"
        ),
        permissions=["tool"],
        extra_frontmatter="dependencies:\n  - dummy_pth_pkg\n",
    )
    skill_dir = repo_root / "env_pth"
    site_dir = (
        skill_dir
        / ".ouroboros_env"
        / "python"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    pkg_dir = site_dir / "dummy_pth_pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("VALUE = 'ok'\n", encoding="utf-8")
    (site_dir / "danger.pth").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('boom', encoding='utf-8')\n",
        encoding="utf-8",
    )
    _mark_isolated_deps_installed(drive_root, loaded)

    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)

    assert err is None, err
    assert not marker.exists()
    extension_loader.unload_extension("env_pth")


def test_isolated_python_deps_do_not_leak_to_other_extensions(tmp_path):
    import sys

    loaded_a, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "env_owner",
        (
            "import shared_pkg\n"
            "def register(api):\n"
            "    api.register_tool('value', lambda ctx: shared_pkg.VALUE, description='value', schema={})\n"
        ),
        permissions=["tool"],
        extra_frontmatter="dependencies:\n  - shared_pkg\n",
    )
    owner_dir = repo_root / "env_owner"
    site_dir = (
        owner_dir
        / ".ouroboros_env"
        / "python"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    pkg_dir = site_dir / "shared_pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("VALUE = 'owned'\n", encoding="utf-8")
    _mark_isolated_deps_installed(drive_root, loaded_a)

    err_a = extension_loader.load_extension(loaded_a, lambda: {}, drive_root=drive_root)
    assert err_a is None, err_a
    tool = extension_loader.get_tool(extension_loader.extension_surface_name("env_owner", "value"))
    assert tool is not None and tool["handler"](None) == "owned"

    loaded_b, _repo_root, _drive_root = _prepare_extension(
        tmp_path,
        "env_neighbor",
        (
            "import shared_pkg\n"
            "def register(api):\n"
            "    api.register_tool('value', lambda ctx: shared_pkg.VALUE, description='value', schema={})\n"
        ),
        permissions=["tool"],
    )
    err_b = extension_loader.load_extension(loaded_b, lambda: {}, drive_root=drive_root)

    assert err_b is not None
    assert "shared_pkg" in err_b
    assert extension_loader.get_tool(extension_loader.extension_surface_name("env_neighbor", "value")) is None
    extension_loader.unload_extension("env_owner")


def test_isolated_namespace_packages_are_purged_after_import_scope(tmp_path):
    import sys

    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "env_namespace",
        (
            "import ns_pkg.sub as sub\n"
            "def register(api):\n"
            "    api.register_tool('value', lambda ctx: sub.VALUE, description='value', schema={})\n"
        ),
        permissions=["tool"],
        extra_frontmatter="dependencies:\n  - ns_pkg\n",
    )
    skill_dir = repo_root / "env_namespace"
    site_dir = (
        skill_dir
        / ".ouroboros_env"
        / "python"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    ns_dir = site_dir / "ns_pkg"
    ns_dir.mkdir(parents=True)
    (ns_dir / "sub.py").write_text("VALUE = 'namespace-ok'\n", encoding="utf-8")
    _mark_isolated_deps_installed(drive_root, loaded)

    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)

    assert err is None, err
    assert "ns_pkg" not in sys.modules
    assert "ns_pkg.sub" not in sys.modules
    extension_loader.unload_extension("env_namespace")


def test_isolated_regular_parent_namespace_child_is_purged_after_import_scope(tmp_path):
    import sys

    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "env_regular_parent_namespace_child",
        (
            "import importlib\n"
            "importlib.import_module('regular_parent_pkg.data')\n"
            "def register(api):\n"
            "    api.register_tool('value', lambda ctx: 'ok', description='value', schema={})\n"
        ),
        permissions=["tool"],
        extra_frontmatter="dependencies:\n  - regular_parent_pkg\n",
    )
    skill_dir = repo_root / "env_regular_parent_namespace_child"
    site_dir = (
        skill_dir
        / ".ouroboros_env"
        / "python"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    pkg_dir = site_dir / "regular_parent_pkg"
    data_dir = pkg_dir / "data"
    data_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("VALUE = 'regular-parent'\n", encoding="utf-8")
    (data_dir / "payload.txt").write_text("namespace-child\n", encoding="utf-8")
    _mark_isolated_deps_installed(drive_root, loaded)

    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)

    assert err is None, err
    assert "regular_parent_pkg" not in sys.modules
    assert "regular_parent_pkg.data" not in sys.modules
    extension_loader.unload_extension("env_regular_parent_namespace_child")


def test_isolated_regular_parent_namespace_child_is_purged_after_async_handler(tmp_path):
    import asyncio
    import sys

    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "env_async_regular_parent_namespace_child",
        (
            "import importlib\n"
            "async def _value(ctx):\n"
            "    module = importlib.import_module('async_regular_parent_pkg.data')\n"
            "    return module.__name__\n"
            "def register(api):\n"
            "    api.register_tool('value', _value, description='value', schema={})\n"
        ),
        permissions=["tool"],
        extra_frontmatter="dependencies:\n  - async_regular_parent_pkg\n",
    )
    skill_dir = repo_root / "env_async_regular_parent_namespace_child"
    site_dir = (
        skill_dir
        / ".ouroboros_env"
        / "python"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    pkg_dir = site_dir / "async_regular_parent_pkg"
    data_dir = pkg_dir / "data"
    data_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("VALUE = 'regular-parent'\n", encoding="utf-8")
    (data_dir / "payload.txt").write_text("namespace-child\n", encoding="utf-8")
    _mark_isolated_deps_installed(drive_root, loaded)

    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    tool = extension_loader.get_tool(
        extension_loader.extension_surface_name("env_async_regular_parent_namespace_child", "value")
    )
    assert tool is not None

    assert asyncio.run(tool["handler"]({})) == "async_regular_parent_pkg.data"
    assert "async_regular_parent_pkg" not in sys.modules
    assert "async_regular_parent_pkg.data" not in sys.modules
    extension_loader.unload_extension("env_async_regular_parent_namespace_child")


def test_isolated_site_scope_releases_execution_lock_when_cleanup_fails(tmp_path, monkeypatch):
    from ouroboros import extension_isolated_deps

    def fail_cleanup(_site_dirs):
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(extension_isolated_deps, "release_isolated_site_dirs", fail_cleanup)

    with extension_isolated_deps.isolated_site_dirs_scope(tmp_path, enabled=False):
        pass

    assert extension_isolated_deps._execution_lock.acquire(blocking=False)
    extension_isolated_deps._execution_lock.release()


def test_async_isolated_site_scope_releases_execution_lock_when_cleanup_fails(tmp_path, monkeypatch):
    import asyncio

    from ouroboros import extension_isolated_deps

    def fail_cleanup(_site_dirs):
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(extension_isolated_deps, "release_isolated_site_dirs", fail_cleanup)

    async def run_scope():
        async with extension_isolated_deps.async_isolated_site_dirs_scope(tmp_path, enabled=False):
            pass

    asyncio.run(run_scope())

    assert extension_isolated_deps._execution_lock.acquire(blocking=False)
    extension_isolated_deps._execution_lock.release()


def test_isolated_site_scope_releases_execution_lock_when_inject_fails(tmp_path, monkeypatch):
    from ouroboros import extension_isolated_deps

    def fail_inject(_skill_dir):
        raise RuntimeError("inject failed")

    monkeypatch.setattr(extension_isolated_deps, "inject_isolated_site_dirs", fail_inject)

    with pytest.raises(RuntimeError, match="inject failed"):
        with extension_isolated_deps.isolated_site_dirs_scope(tmp_path, enabled=True):
            pass

    assert extension_isolated_deps._execution_lock.acquire(blocking=False)
    extension_isolated_deps._execution_lock.release()


def test_async_isolated_site_scope_releases_execution_lock_when_inject_fails(tmp_path, monkeypatch):
    import asyncio

    from ouroboros import extension_isolated_deps

    def fail_inject(_skill_dir):
        raise RuntimeError("inject failed")

    monkeypatch.setattr(extension_isolated_deps, "inject_isolated_site_dirs", fail_inject)

    async def run_scope():
        async with extension_isolated_deps.async_isolated_site_dirs_scope(tmp_path, enabled=True):
            pass

    with pytest.raises(RuntimeError, match="inject failed"):
        asyncio.run(run_scope())

    assert extension_isolated_deps._execution_lock.acquire(blocking=False)
    extension_isolated_deps._execution_lock.release()


def test_async_isolated_site_scope_cancel_while_waiting_does_not_wedge_lock(tmp_path):
    import asyncio

    from ouroboros import extension_isolated_deps

    async def run_scope():
        async with extension_isolated_deps.async_isolated_site_dirs_scope(tmp_path, enabled=False):
            return "entered"

    async def main():
        assert extension_isolated_deps._execution_lock.acquire(blocking=False)
        task = asyncio.create_task(run_scope())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        extension_isolated_deps._execution_lock.release()
        await asyncio.sleep(0.05)
        assert extension_isolated_deps._execution_lock.acquire(blocking=False)
        extension_isolated_deps._execution_lock.release()

    asyncio.run(main())


def test_release_isolated_site_dirs_removes_path_when_module_scan_fails(tmp_path, monkeypatch):
    import sys
    import types

    from ouroboros import extension_isolated_deps

    skill_dir = tmp_path / "skill"
    site_dir = (
        skill_dir
        / ".ouroboros_env"
        / "python"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    site_dir.mkdir(parents=True)
    site_str = str(site_dir.resolve())

    injected = extension_isolated_deps.inject_isolated_site_dirs(skill_dir)
    assert injected == [site_str]
    assert site_str in sys.path
    assert extension_isolated_deps._injected_site_dir_refs.get(site_str) == 1
    module_name = "scan_failure_pkg"
    module = types.ModuleType(module_name)
    module.__file__ = str(site_dir / "scan_failure_pkg.py")
    sys.modules[module_name] = module

    def fail_scan(_site_path):
        raise RuntimeError("module scan failed")

    monkeypatch.setattr(extension_isolated_deps, "_module_names_under_site_dir", fail_scan)

    extension_isolated_deps.release_isolated_site_dirs(injected)

    assert site_str not in sys.path
    assert site_str not in extension_isolated_deps._injected_site_dir_refs
    assert module_name not in sys.modules


def test_release_isolated_site_dirs_removes_preexisting_env_parent_path(tmp_path):
    import sys

    from ouroboros import extension_isolated_deps

    skill_dir = tmp_path / "skill"
    env_parent = skill_dir / ".ouroboros_env" / "python"
    site_dir = (
        env_parent
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    site_dir.mkdir(parents=True)
    env_parent_str = str(env_parent.resolve())
    site_str = str(site_dir.resolve())
    sys.path.insert(0, env_parent_str)
    try:
        injected = extension_isolated_deps.inject_isolated_site_dirs(skill_dir)
        assert injected == [site_str]
        extension_isolated_deps.release_isolated_site_dirs(injected)
        assert env_parent_str not in sys.path
        assert site_str not in sys.path
    finally:
        while env_parent_str in sys.path:
            sys.path.remove(env_parent_str)
        while site_str in sys.path:
            sys.path.remove(site_str)
        extension_isolated_deps._injected_site_dir_refs.pop(site_str, None)


def test_inject_isolated_site_dirs_tracks_preexisting_env_path(tmp_path):
    import sys

    from ouroboros import extension_isolated_deps

    skill_dir = tmp_path / "skill"
    site_dir = (
        skill_dir
        / ".ouroboros_env"
        / "python"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    site_dir.mkdir(parents=True)
    site_str = str(site_dir.resolve())
    sys.path.insert(0, site_str)
    try:
        injected = extension_isolated_deps.inject_isolated_site_dirs(skill_dir)
        assert injected == [site_str]
        assert extension_isolated_deps._injected_site_dir_refs.get(site_str) == 1
        extension_isolated_deps.release_isolated_site_dirs(injected)
        assert site_str not in sys.path
        assert site_str not in extension_isolated_deps._injected_site_dir_refs
    finally:
        while site_str in sys.path:
            sys.path.remove(site_str)
        extension_isolated_deps._injected_site_dir_refs.pop(site_str, None)


def test_isolated_python_deps_do_not_leak_during_overlapping_handlers(tmp_path):
    import sys
    import threading

    started = threading.Event()
    release = threading.Event()
    loaded_a, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "env_overlap_owner",
        (
            "def _slow(ctx):\n"
            "    ctx['started'].set()\n"
            "    ctx['release'].wait(2)\n"
            "    import overlap_pkg\n"
            "    return overlap_pkg.VALUE\n"
            "def register(api):\n"
            "    api.register_tool('slow', _slow, description='slow', schema={})\n"
        ),
        permissions=["tool"],
        extra_frontmatter="dependencies:\n  - overlap_pkg\n",
    )
    owner_dir = repo_root / "env_overlap_owner"
    site_dir = (
        owner_dir
        / ".ouroboros_env"
        / "python"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    pkg_dir = site_dir / "overlap_pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("VALUE = 'owner'\n", encoding="utf-8")
    _mark_isolated_deps_installed(drive_root, loaded_a)
    err_a = extension_loader.load_extension(loaded_a, lambda: {}, drive_root=drive_root)
    assert err_a is None, err_a
    tool = extension_loader.get_tool(extension_loader.extension_surface_name("env_overlap_owner", "slow"))
    assert tool is not None

    loaded_b, _repo_root, _drive_root = _prepare_extension(
        tmp_path,
        "env_overlap_neighbor",
        (
            "import overlap_pkg\n"
            "def register(api):\n"
            "    api.register_tool('value', lambda ctx: overlap_pkg.VALUE, description='value', schema={})\n"
        ),
        permissions=["tool"],
    )
    slow_result = {}
    neighbor_result = {}

    def run_slow():
        slow_result["value"] = tool["handler"]({"started": started, "release": release})

    def load_neighbor():
        neighbor_result["err"] = extension_loader.load_extension(loaded_b, lambda: {}, drive_root=drive_root)

    t1 = threading.Thread(target=run_slow)
    t1.start()
    assert started.wait(2)
    t2 = threading.Thread(target=load_neighbor)
    t2.start()
    t2.join(timeout=0.1)
    assert t2.is_alive()
    release.set()
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert slow_result["value"] == "owner"
    assert neighbor_result["err"] is not None
    assert "overlap_pkg" in neighbor_result["err"]
    extension_loader.unload_extension("env_overlap_owner")


def test_isolated_python_deps_do_not_leak_during_overlapping_async_handlers(tmp_path):
    import asyncio
    import sys

    loaded_a, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "env_async_owner",
        (
            "async def _slow(ctx):\n"
            "    ctx['started'].set()\n"
            "    await ctx['release'].wait()\n"
            "    import async_pkg\n"
            "    return async_pkg.VALUE\n"
            "def register(api):\n"
            "    api.register_tool('slow', _slow, description='slow', schema={})\n"
        ),
        permissions=["tool"],
        extra_frontmatter="dependencies:\n  - async_pkg\n",
    )
    owner_dir = repo_root / "env_async_owner"
    site_dir = (
        owner_dir
        / ".ouroboros_env"
        / "python"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    pkg_dir = site_dir / "async_pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("VALUE = 'async-owner'\n", encoding="utf-8")
    _mark_isolated_deps_installed(drive_root, loaded_a)

    loaded_b, _repo_root, _drive_root = _prepare_extension(
        tmp_path,
        "env_async_neighbor",
        (
            "async def _value(ctx):\n"
            "    import async_pkg\n"
            "    return async_pkg.VALUE\n"
            "def register(api):\n"
            "    api.register_tool('value', _value, description='value', schema={})\n"
        ),
        permissions=["tool"],
    )
    assert extension_loader.load_extension(loaded_a, lambda: {}, drive_root=drive_root) is None
    assert extension_loader.load_extension(loaded_b, lambda: {}, drive_root=drive_root) is None
    tool_a = extension_loader.get_tool(extension_loader.extension_surface_name("env_async_owner", "slow"))
    tool_b = extension_loader.get_tool(extension_loader.extension_surface_name("env_async_neighbor", "value"))

    async def main():
        started = asyncio.Event()
        release = asyncio.Event()
        task_a = asyncio.create_task(tool_a["handler"]({"started": started, "release": release}))
        await asyncio.wait_for(started.wait(), timeout=2)
        task_b = asyncio.create_task(tool_b["handler"]({}))
        await asyncio.sleep(0.05)
        assert not task_b.done()
        release.set()
        assert await asyncio.wait_for(task_a, timeout=2) == "async-owner"
        with pytest.raises(ModuleNotFoundError):
            await asyncio.wait_for(task_b, timeout=2)

    asyncio.run(main())
    extension_loader.unload_extension("env_async_owner")
    extension_loader.unload_extension("env_async_neighbor")


def test_load_extension_registers_route_with_prefix(tmp_path):
    plugin = (
        "def _handler(request): return {'ok': True}\n"
        "def register(api):\n"
        "    api.register_route('weather', _handler, methods=('GET',))\n"
    )
    loaded, _, drive_root = _prepare_extension(tmp_path, "ext2", plugin, permissions=["route"])
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    snap = extension_loader.snapshot()
    assert "/api/extensions/ext2/weather" in snap["routes"]


_ROUTE_REJECTION_CASES = [
    (
        "absolute_route",
        "ext_abs",
        "def _handler(r): return {}\n"
        "def register(api):\n"
        "    api.register_route('/absolute', _handler)\n",
        "absolute",
    ),
    (
        "traversal_route",
        "ext_traverse",
        "def _handler(r): return {}\n"
        "def register(api):\n"
        "    api.register_route('../escape', _handler)\n",
        None,
    ),
    (
        "unsupported_method",
        "ext_trace",
        "def _handler(r): return {}\n"
        "def register(api):\n"
        "    api.register_route('weather', _handler, methods=('TRACE',))\n",
        "unsupported",
    ),
]


@pytest.mark.parametrize(
    "case_id,name,plugin,expected_substr",
    _ROUTE_REJECTION_CASES,
    ids=[c[0] for c in _ROUTE_REJECTION_CASES],
)
def test_load_extension_rejects_route(tmp_path, case_id, name, plugin, expected_substr):
    loaded, _, drive_root = _prepare_extension(tmp_path, name, plugin, permissions=["route"])
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is not None
    if expected_substr is not None:
        assert expected_substr in err.lower()


def test_load_extension_accepts_string_route_method(tmp_path):
    plugin = (
        "def _handler(r): return {}\n"
        "def register(api):\n"
        "    api.register_route('weather', _handler, methods='GET')\n"
    )
    loaded, _, drive_root = _prepare_extension(tmp_path, "ext_get_string", plugin, permissions=["route"])
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    snap = extension_loader.snapshot()
    assert "/api/extensions/ext_get_string/weather" in snap["routes"]


def test_load_extension_supports_nested_entry_relative_imports(tmp_path):
    repo_root = tmp_path / "skills"
    skill_dir = _write_ext_skill(
        repo_root,
        "ext_nested",
        permissions=["tool"],
        entry="pkg/plugin.py",
        plugin_body=(
            "from .helper import VALUE\n"
            "def register(api):\n"
            "    api.register_tool('t', lambda ctx: VALUE, description='', schema={})\n"
        ),
    )
    (skill_dir / "pkg" / "helper.py").write_text("VALUE = 'nested-ok'\n", encoding="utf-8")
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    save_enabled(drive_root, "ext_nested", True)
    content_hash = compute_content_hash(skill_dir, manifest_entry="pkg/plugin.py")
    save_review_state(
        drive_root,
        "ext_nested",
        SkillReviewState(status="pass", content_hash=content_hash),
    )
    loaded = find_skill(drive_root, "ext_nested", repo_path=str(repo_root))
    assert loaded is not None
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    tool = extension_loader.get_tool(extension_loader.extension_surface_name("ext_nested", "t"))
    assert tool is not None
    assert tool["handler"](None) == "nested-ok"


def test_unload_dotted_prefix_skill_does_not_break_neighbor_imports(tmp_path):
    repo_root = tmp_path / "skills"
    foo_dir = _write_ext_skill(
        repo_root,
        "foo",
        permissions=["tool"],
        plugin_body=(
            "def register(api):\n"
            "    api.register_tool('t', lambda ctx: 'foo', description='', schema={})\n"
        ),
    )
    dotted_dir = _write_ext_skill(
        repo_root,
        "foo.bar",
        permissions=["tool"],
        plugin_body=(
            "def _lazy(ctx):\n"
            "    from .helper import VALUE\n"
            "    return VALUE\n"
            "def register(api):\n"
            "    api.register_tool('lazy', _lazy, description='', schema={})\n"
        ),
    )
    (dotted_dir / "helper.py").write_text("VALUE = 'still-live'\n", encoding="utf-8")
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    for name, skill_dir in (("foo", foo_dir), ("foo.bar", dotted_dir)):
        save_enabled(drive_root, name, True)
        save_review_state(
            drive_root,
            name,
            SkillReviewState(status="pass", content_hash=compute_content_hash(skill_dir, manifest_entry="plugin.py")),
        )
        loaded = find_skill(drive_root, name, repo_path=str(repo_root))
        assert loaded is not None
        assert extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root) is None

    extension_loader.unload_extension("foo")
    tool = extension_loader.get_tool(extension_loader.extension_surface_name("foo.bar", "lazy"))
    assert tool is not None
    assert tool["handler"](None) == "still-live"


def test_load_extension_registers_ws_handler_with_namespace(tmp_path):
    plugin = (
        "async def _handler(payload):\n"
        "    return {'acked': True}\n"
        "def register(api):\n"
        "    api.register_ws_handler('message', _handler)\n"
    )
    loaded, _, drive_root = _prepare_extension(tmp_path, "ws1", plugin, permissions=["ws_handler"])
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    handlers = extension_loader.list_ws_handlers()
    assert extension_loader.extension_surface_name("ws1", "message") in handlers


def test_send_ws_message_broadcasts_namespaced_event(tmp_path):
    sent: list[dict] = []
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "push_ext",
        "def register(api):\n"
        "    api.send_ws_message('progress', {'pct': 40})\n",
        permissions=["ws_handler"],
    )
    extension_loader.set_ws_broadcaster(sent.append)

    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)

    assert err is None, err
    assert sent == [
        {
            "type": extension_loader.extension_surface_name("push_ext", "progress"),
            "data": {"pct": 40},
            "skill": "push_ext",
        }
    ]


def test_send_ws_message_still_works_after_registration_phase(tmp_path):
    sent: list[dict] = []
    impl = extension_loader.PluginAPIImpl(
        skill_name="push_runtime",
        permissions=["ws_handler"],
        env_allowlist=[],
        state_dir=tmp_path,
        settings_reader=lambda: {},
    )
    extension_loader.set_ws_broadcaster(sent.append)

    impl._close_registration()
    impl.send_ws_message("progress", {"pct": 90})

    assert sent[0]["type"] == extension_loader.extension_surface_name("push_runtime", "progress")
    assert sent[0]["data"] == {"pct": 90}


def test_send_ws_message_requires_ws_permission(tmp_path):
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "no_push_ext",
        "def register(api):\n"
        "    api.send_ws_message('progress', {'pct': 40})\n",
        permissions=[],
    )

    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)

    assert err is not None
    assert "ws_handler" in err


def test_register_ui_tab_surfaces_hostable_widget(tmp_path):
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "uiwait",
        "def register(api):\n"
        "    api.register_ui_tab('weather', 'Weather', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'markdown', 'text': 'ok'}]})\n",
        permissions=["widget"],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    snap = extension_loader.snapshot()
    assert snap["ui_tabs_pending"] == []
    assert snap["ui_tabs"][0]["key"] == "uiwait:weather"
    assert snap["ui_tabs"][0]["ws_prefix"] == extension_loader.extension_name_prefix("uiwait")
    assert snap["ui_tabs"][0]["render"]["kind"] == "declarative"
    assert snap["ui_tabs"][0]["span"] == 1
    assert snap["ui_tabs"][0]["grid_span"] == 1

    extension_loader.unload_extension("uiwait")
    snap = extension_loader.snapshot()
    assert snap["ui_tabs"] == []


def test_register_ui_tab_snapshots_nested_render_dicts(tmp_path):
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "uicopy",
        "_RENDER = {'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'markdown', 'text': 'ok'}]}\n"
        "def register(api):\n"
        "    api.register_ui_tab('weather', 'Weather', render=_RENDER)\n"
        "    _RENDER['components'][0]['text'] = 'mutated after registration'\n",
        permissions=["widget"],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err

    snap = extension_loader.snapshot()
    assert snap["ui_tabs"][0]["render"]["components"][0]["text"] == "ok"
    snap["ui_tabs"][0]["render"]["components"][0]["text"] = "mutated by caller"
    assert (
        extension_loader.snapshot()["ui_tabs"][0]["render"]["components"][0]["text"]
        == "ok"
    )

    extension_loader.unload_extension("uicopy")


def test_register_ui_tab_promotes_render_span_metadata(tmp_path):
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "wideui",
        "def register(api):\n"
        "    api.register_ui_tab('wide', 'Wide', render={'kind': 'declarative', 'schema_version': 1, 'span': 2, 'components': [{'type': 'markdown', 'text': 'ok'}]})\n",
        permissions=["widget"],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    snap = extension_loader.snapshot()
    assert snap["ui_tabs"][0]["span"] == 2
    assert snap["ui_tabs"][0]["grid_span"] == 2
    assert snap["ui_tabs"][0]["render"]["span"] == 2

    extension_loader.unload_extension("wideui")


_UI_TAB_REJECTION_CASES = [
    (
        "unsupported_render_kind",
        "badui",
        "def register(api):\n"
        "    api.register_ui_tab('bad', 'Bad', render={'kind': 'script_module', 'src': 'x.js'})\n",
        "unsupported",
    ),
    (
        "bad_declarative_component",
        "baddecl",
        "def register(api):\n"
        "    api.register_ui_tab('bad', 'Bad', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'script'}]})\n",
        "unsupported type",
    ),
    (
        "declarative_form_without_route",
        "badform",
        "def register(api):\n"
        "    api.register_ui_tab('bad', 'Bad', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'form', 'fields': [{'name': 'q'}]}]})\n",
        "requires route or api_route",
    ),
    (
        "declarative_table_without_columns",
        "badtable",
        "def register(api):\n"
        "    api.register_ui_tab('bad', 'Bad', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'table', 'path': 'rows'}]})\n",
        "columns",
    ),
    (
        "declarative_media_without_source",
        "badmedia",
        "def register(api):\n"
        "    api.register_ui_tab('bad', 'Bad', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'image', 'label': 'Preview'}]})\n",
        "media source",
    ),
    (
        "bad_gallery_item",
        "badgallery",
        "def register(api):\n"
        "    api.register_ui_tab('bad', 'Bad', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'gallery', 'items': [None]}]})\n",
        "item 0 must be an object",
    ),
    (
        "non_object_render",
        "baduirender",
        "def register(api):\n"
        "    api.register_ui_tab('bad', 'Bad', render=[])\n",
        "ui render must be an object",
    ),
]


def test_register_ui_tab_accepts_declarative_poll_component(tmp_path):
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "pollui",
        "def register(api):\n"
        "    api.register_ui_tab('poll', 'Poll', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'poll', 'route': 'status', 'auto_start': True}]})\n",
        permissions=["widget"],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    snap = extension_loader.snapshot()
    assert snap["ui_tabs"][0]["render"]["components"][0]["type"] == "poll"
    assert snap["ui_tabs"][0]["render"]["components"][0]["auto_start"] is True


def test_register_ui_tab_accepts_subscription_component(tmp_path):
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "subui",
        "def register(api):\n"
        "    api.register_ui_tab('sub', 'Sub', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'subscription', 'event': 'progress', 'target': 'result'}, {'type': 'progress', 'path': 'pct'}]})\n",
        permissions=["widget"],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    snap = extension_loader.snapshot()
    assert snap["ui_tabs"][0]["render"]["components"][0]["type"] == "subscription"


def test_register_ui_tab_accepts_subscription_render_children(tmp_path):
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "subrender",
        "def register(api):\n"
        "    api.register_ui_tab('sub', 'Sub', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'subscription', 'event': 'progress', 'target': 'result', 'render': [{'type': 'progress', 'value_key': 'progress_pct', 'label_key': 'message'}, {'type': 'gallery', 'items_key': 'frames', 'item_type': 'image', 'route_prefix': 'asset?path='}, {'type': 'key_value', 'items_key': 'stats'}]}]})\n",
        permissions=["widget"],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    component = extension_loader.snapshot()["ui_tabs"][0]["render"]["components"][0]
    assert component["type"] == "subscription"
    assert [item["type"] for item in component["render"]] == ["progress", "gallery", "key_value"]


def test_register_ui_tab_accepts_widget_v2_components(tmp_path):
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "v2ui",
        "def register(api):\n"
        "    api.register_ui_tab('v2', 'V2', render={'kind': 'declarative', 'schema_version': 1, 'components': [\n"
        "        {'type': 'code', 'text': 'print(1)'},\n"
        "        {'type': 'chart', 'path': 'chart'},\n"
        "        {'type': 'tabs', 'tabs': [{'label': 'A', 'components': [{'type': 'markdown', 'text': 'ok'}]}]},\n"
        "        {'type': 'stream', 'route': 'events'}\n"
        "    ]})\n",
        permissions=["widget"],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    types = [item["type"] for item in extension_loader.snapshot()["ui_tabs"][0]["render"]["components"]]
    assert types == ["code", "chart", "tabs", "stream"]


_UI_TAB_REJECTION_CASES.extend([
    (
        "bad_tabs_component",
        "badtabs",
        "def register(api):\n"
        "    api.register_ui_tab('tabs', 'Tabs', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'tabs', 'tabs': []}]})\n",
        "tabs",
    ),
    (
        "invalid_nested_tab_component",
        "badnestedtabs",
        "def register(api):\n"
        "    api.register_ui_tab('tabs', 'Tabs', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'tabs', 'tabs': [{'label': 'A', 'components': [{'type': 'image'}]}]}]})\n",
        "media source",
    ),
    (
        "interactive_nested_tab_component",
        "badinteractivetabs",
        "def register(api):\n"
        "    api.register_ui_tab('tabs', 'Tabs', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'tabs', 'tabs': [{'label': 'A', 'components': [{'type': 'form', 'route': 'submit', 'fields': [{'name': 'q'}]}]}]}]})\n",
        "interactive type",
    ),
    (
        "nested_tabs_component",
        "badnestednestedtabs",
        "def register(api):\n"
        "    api.register_ui_tab('tabs', 'Tabs', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'tabs', 'tabs': [{'label': 'A', 'components': [{'type': 'tabs', 'tabs': [{'label': 'B', 'components': []}]}]}]}]})\n",
        "interactive type",
    ),
    (
        "stream_without_route",
        "badstream",
        "def register(api):\n"
        "    api.register_ui_tab('stream', 'Stream', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'stream'}]})\n",
        "requires route",
    ),
    (
        "stream_with_non_get_method",
        "badstreammethod",
        "def register(api):\n"
        "    api.register_ui_tab('stream', 'Stream', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'stream', 'route': 'events', 'method': 'POST'}]})\n",
        "stream method",
    ),
    (
        "subscription_without_event",
        "badsubui",
        "def register(api):\n"
        "    api.register_ui_tab('sub', 'Sub', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'subscription'}]})\n",
        "requires event",
    ),
])


@pytest.mark.parametrize(
    "case_id,name,plugin,expected_substr",
    _UI_TAB_REJECTION_CASES,
    ids=[c[0] for c in _UI_TAB_REJECTION_CASES],
)
def test_register_ui_tab_rejects(tmp_path, case_id, name, plugin, expected_substr):
    loaded, _, drive_root = _prepare_extension(tmp_path, name, plugin, permissions=["widget"])
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is not None
    assert expected_substr in err


def test_on_unload_callback_runs_during_unload(tmp_path):
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "cleanup_ext",
        "import pathlib\n"
        "def register(api):\n"
        "    state_dir = pathlib.Path(api.get_state_dir())\n"
        "    api.on_unload(lambda: (state_dir / 'cleanup.txt').write_text('done', encoding='utf-8'))\n"
        "    api.register_tool('ping', lambda **kw: 'pong', description='ping', schema={})\n",
        permissions=["tool"],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err

    extension_loader.unload_extension("cleanup_ext")

    cleanup_file = drive_root / "state" / "skills" / "cleanup_ext" / "cleanup.txt"
    assert cleanup_file.read_text(encoding="utf-8") == "done"
    assert extension_loader.snapshot()["tools"] == []


def test_on_unload_callback_error_does_not_block_teardown(tmp_path):
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "bad_cleanup_ext",
        "def register(api):\n"
        "    api.on_unload(lambda: (_ for _ in ()).throw(RuntimeError('boom')))\n"
        "    api.register_tool('ping', lambda **kw: 'pong', description='ping', schema={})\n",
        permissions=["tool"],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err

    extension_loader.unload_extension("bad_cleanup_ext")

    assert extension_loader.snapshot()["tools"] == []


def test_on_unload_callback_cannot_reregister_surfaces(tmp_path):
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "ghost_ext",
        "def register(api):\n"
        "    api.on_unload(lambda: api.register_tool('ghost', lambda **kw: 'boo', description='ghost', schema={}))\n"
        "    api.register_tool('ping', lambda **kw: 'pong', description='ping', schema={})\n",
        permissions=["tool"],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err

    extension_loader.unload_extension("ghost_ext")

    snap = extension_loader.snapshot()
    assert snap["extensions"] == []
    assert snap["tools"] == []


def test_on_unload_delayed_callback_cannot_reregister_surfaces(tmp_path):
    import time

    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "delayed_ghost_ext",
        "import threading, time\n"
        "def register(api):\n"
        "    def cleanup():\n"
        "        def later():\n"
        "            time.sleep(0.1)\n"
        "            try:\n"
        "                api.register_tool('ghost', lambda **kw: 'boo', description='ghost', schema={})\n"
        "            except Exception:\n"
        "                pass\n"
        "        threading.Thread(target=later).start()\n"
        "    api.on_unload(cleanup)\n"
        "    api.register_tool('ping', lambda **kw: 'pong', description='ping', schema={})\n",
        permissions=["tool"],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err

    extension_loader.unload_extension("delayed_ghost_ext")
    time.sleep(0.3)

    snap = extension_loader.snapshot()
    assert snap["extensions"] == []
    assert snap["tools"] == []


def test_delayed_post_load_registration_is_rejected(tmp_path):
    import time

    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "late_register_ext",
        "import threading, time\n"
        "def register(api):\n"
        "    api.register_tool('ping', lambda **kw: 'pong', description='ping', schema={})\n"
        "    def later():\n"
        "        time.sleep(0.1)\n"
        "        try:\n"
        "            api.register_tool('ghost', lambda **kw: 'boo', description='ghost', schema={})\n"
        "        except Exception:\n"
        "            pass\n"
        "    threading.Thread(target=later).start()\n",
        permissions=["tool"],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    time.sleep(0.3)

    snap = extension_loader.snapshot()
    assert snap["tools"] == [extension_loader.extension_surface_name("late_register_ext", "ping")]


def test_reconcile_unload_callbacks_do_not_hold_loader_lock(tmp_path):
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "lock_probe",
        "import pathlib, threading\n"
        "def register(api):\n"
        "    state_dir = pathlib.Path(api.get_state_dir())\n"
        "    def cleanup():\n"
        "        done = state_dir / 'snapshot_done.txt'\n"
        "        def worker():\n"
        "            from ouroboros import extension_loader\n"
        "            extension_loader.snapshot()\n"
        "            done.write_text('done', encoding='utf-8')\n"
        "        thread = threading.Thread(target=worker)\n"
        "        thread.start()\n"
        "        thread.join(timeout=1.0)\n"
        "        if not done.exists():\n"
        "            raise RuntimeError('snapshot blocked by loader lock')\n"
        "    api.on_unload(cleanup)\n"
        "    api.register_tool('ping', lambda **kw: 'pong', description='ping', schema={})\n",
        permissions=["tool"],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err

    # Make the extension undesired so reconcile unloads it through the normal path.
    save_enabled(drive_root, "lock_probe", False)
    state = extension_loader.reconcile_extension("lock_probe", drive_root, lambda: {})

    done_file = drive_root / "state" / "skills" / "lock_probe" / "snapshot_done.txt"
    assert done_file.read_text(encoding="utf-8") == "done"
    assert state["action"] == "extension_unloaded"


def test_concurrent_reconcile_converges_to_one_live_extension(tmp_path):
    import threading

    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "race_ext",
        "import time\n"
        "def register(api):\n"
        "    time.sleep(0.05)\n"
        "    api.register_tool('ping', lambda **kw: 'pong', description='ping', schema={})\n",
        permissions=["tool"],
    )

    results = []
    repo_path = str(tmp_path / "skills")
    threads = [
        threading.Thread(
            target=lambda: results.append(
                extension_loader.reconcile_extension("race_ext", drive_root, lambda: {}, repo_path=repo_path)
            )
        )
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2.0)

    assert len(results) == 2
    assert {r["action"] for r in results} <= {"extension_loaded", "extension_already_live"}
    snap = extension_loader.snapshot()
    assert snap["extensions"] == ["race_ext"]
    assert snap["tools"] == [extension_loader.extension_surface_name("race_ext", "ping")]
    assert extension_loader.runtime_state_for_skill_name("race_ext", drive_root, repo_path=repo_path)["reason"] == "ready"


def test_reconcile_extension_allows_warnings_review(tmp_path, monkeypatch):
    from ouroboros.skill_loader import find_skill
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")

    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "advisory_live",
        "def register(api):\n"
        "    api.register_tool('ping', lambda **kw: 'pong', description='ping', schema={})\n",
        permissions=["tool"],
    )
    save_review_state(
        drive_root,
        "advisory_live",
        SkillReviewState(status="warnings", content_hash=loaded.content_hash),
    )
    loaded = find_skill(drive_root, "advisory_live", repo_path=str(repo_root))
    assert loaded is not None

    state = extension_loader.reconcile_extension(
        "advisory_live",
        drive_root,
        lambda: {},
        repo_path=str(repo_root),
    )

    assert state["action"] == "extension_loaded"
    assert extension_loader.runtime_state_for_skill_name(
        "advisory_live",
        drive_root,
        repo_path=str(repo_root),
    )["reason"] == "ready"


def test_reconcile_extension_allows_warnings_under_blocking(tmp_path, monkeypatch):
    from ouroboros.skill_loader import find_skill
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking")

    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "advisory_warnings",
        "def register(api):\n"
        "    api.register_tool('ping', lambda **kw: 'pong', description='ping', schema={})\n",
        permissions=["tool"],
    )
    save_review_state(
        drive_root,
        "advisory_warnings",
        SkillReviewState(status="warnings", content_hash=loaded.content_hash),
    )
    loaded = find_skill(drive_root, "advisory_warnings", repo_path=str(repo_root))
    assert loaded is not None

    state = extension_loader.reconcile_extension(
        "advisory_warnings",
        drive_root,
        lambda: {},
        repo_path=str(repo_root),
    )

    assert state["action"] == "extension_loaded"
    assert state["reason"] == "ready"


def test_load_extension_permission_gate_tool(tmp_path):
    """Extension without 'tool' permission cannot register a tool."""
    plugin = (
        "def _h(ctx): return 'ok'\n"
        "def register(api):\n"
        "    api.register_tool('x', _h, description='', schema={})\n"
    )
    loaded, _, drive_root = _prepare_extension(tmp_path, "nopoerm", plugin, permissions=["route"])
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is not None
    assert "'tool'" in err


def test_load_extension_enforces_review_pass(tmp_path):
    """Unreviewed extension is refused (after being enabled)."""
    from ouroboros.skill_loader import find_skill
    repo_root = tmp_path / "skills"
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    plugin = "def register(api): pass\n"
    _write_ext_skill(repo_root, "unreviewed", plugin_body=plugin, permissions=[])
    # Enable to get past the "disabled" gate — we want to exercise the
    # review-status gate specifically.
    save_enabled(drive_root, "unreviewed", True)
    loaded = find_skill(drive_root, "unreviewed", repo_path=str(repo_root))
    assert loaded is not None
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is not None
    assert "executable review" in err


def test_load_extension_refuses_disabled(tmp_path):
    from ouroboros.skill_loader import find_skill
    repo_root = tmp_path / "skills"
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    plugin = "def register(api): pass\n"
    _write_ext_skill(repo_root, "d1", plugin_body=plugin, permissions=[])
    loaded = find_skill(drive_root, "d1", repo_path=str(repo_root))
    assert loaded is not None
    save_review_state(
        drive_root,
        "d1",
        SkillReviewState(status="pass", content_hash=loaded.content_hash),
    )
    # NOT enabled.
    loaded = find_skill(drive_root, "d1", repo_path=str(repo_root))
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is not None
    assert "disabled" in err


def test_reconcile_extension_stays_loaded_in_light_mode(tmp_path, monkeypatch):
    """v5.1.2 Frame A: ``light`` no longer unloads extensions. The
    ``runtime_mode_light`` reason is gone from
    ``_extension_runtime_state``. Extensions follow the same
    enabled / review / content-hash gates regardless of mode.
    """
    plugin = (
        "def _echo(ctx):\n"
        "    return 'ok'\n"
        "def register(api):\n"
        "    api.register_tool('echo', _echo, description='echo', schema={})\n"
    )
    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "lightstop",
        plugin,
        permissions=["tool"],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    assert "lightstop" in extension_loader.snapshot()["extensions"]

    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    state = extension_loader.reconcile_extension(
        "lightstop",
        drive_root,
        lambda: {},
        repo_path=repo_root,
    )
    # The ``runtime_mode_light`` reason was removed in v5.1.2; the
    # extension stays live.
    assert state["reason"] != "runtime_mode_light"
    assert state["action"] != "extension_unloaded"
    assert "lightstop" in extension_loader.snapshot()["extensions"]


def test_reconcile_extension_keeps_live_extension_loaded(tmp_path, monkeypatch):
    plugin = (
        "def _echo(ctx):\n"
        "    return 'ok'\n"
        "def register(api):\n"
        "    api.register_tool('echo', _echo, description='echo', schema={})\n"
    )
    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "steady",
        plugin,
        permissions=["tool"],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    unload_calls: list[str] = []
    monkeypatch.setattr(extension_loader, "unload_extension", unload_calls.append)

    state = extension_loader.reconcile_extension(
        "steady",
        drive_root,
        lambda: {},
        repo_path=repo_root,
    )
    assert state["reason"] == "ready"
    assert state["action"] == "extension_already_live"
    assert unload_calls == []
    assert "steady" in extension_loader.snapshot()["extensions"]


def test_reconcile_extension_reloads_when_live_code_changes(tmp_path):
    from ouroboros.skill_loader import find_skill

    skill_dir = tmp_path / "skills" / "reloadme"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            "name: reloadme\n"
            "description: Live reload.\n"
            "version: 0.1.0\n"
            "type: extension\n"
            "entry: plugin.py\n"
            "permissions: [\"tool\"]\n"
            "env_from_settings: []\n"
            "---\n"
            "body\n"
        ),
        encoding="utf-8",
    )
    (skill_dir / "plugin.py").write_text(
        (
            "def _echo(ctx):\n"
            "    return 'v1'\n"
            "def register(api):\n"
            "    api.register_tool('echo', _echo, description='echo', schema={})\n"
        ),
        encoding="utf-8",
    )
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    save_enabled(drive_root, "reloadme", True)
    loaded = find_skill(drive_root, "reloadme", repo_path=str(skill_dir.parent))
    assert loaded is not None
    save_review_state(
        drive_root,
        "reloadme",
        SkillReviewState(status="pass", content_hash=loaded.content_hash),
    )
    loaded = find_skill(drive_root, "reloadme", repo_path=str(skill_dir.parent))
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    tool = extension_loader.get_tool(extension_loader.extension_surface_name("reloadme", "echo"))
    assert tool is not None
    assert tool["handler"](None) == "v1"

    (skill_dir / "plugin.py").write_text(
        (
            "def _echo(ctx):\n"
            "    return 'v2'\n"
            "def register(api):\n"
            "    api.register_tool('echo', _echo, description='echo', schema={})\n"
        ),
        encoding="utf-8",
    )
    loaded = find_skill(drive_root, "reloadme", repo_path=str(skill_dir.parent))
    assert loaded is not None
    save_review_state(
        drive_root,
        "reloadme",
        SkillReviewState(status="pass", content_hash=loaded.content_hash),
    )

    state = extension_loader.reconcile_extension(
        "reloadme",
        drive_root,
        lambda: {},
        repo_path=skill_dir.parent,
        retry_load_error=True,
    )
    assert state["action"] == "extension_loaded"
    assert state["live_loaded"] is True
    tool = extension_loader.get_tool(extension_loader.extension_surface_name("reloadme", "echo"))
    assert tool is not None
    assert tool["handler"](None) == "v2"


def test_runtime_state_preserves_matching_load_error(tmp_path):
    plugin = (
        "def _hello(request):\n"
        "    return {'hello': 'world'}\n"
        "def register(api):\n"
        "    api.register_route('/absolute', _hello, methods=('GET',))\n"
    )
    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "brokenlive",
        plugin,
        permissions=["route"],
    )
    state = extension_loader.reconcile_extension(
        "brokenlive",
        drive_root,
        lambda: {},
        repo_path=repo_root,
        retry_load_error=True,
    )
    assert state["action"] == "extension_load_error"
    refreshed = extension_loader.runtime_state_for_skill_name(
        "brokenlive",
        drive_root,
        repo_path=repo_root,
    )
    assert refreshed["reason"] == "load_error"
    assert "absolute" in str(refreshed["load_error"])
    assert refreshed["live_loaded"] is False


def test_runtime_state_for_skill_name_reports_missing_skill(tmp_path):
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    state = extension_loader.runtime_state_for_skill_name(
        "ghost",
        drive_root,
        repo_path=tmp_path / "skills",
    )
    assert state["desired_live"] is False
    assert state["live_loaded"] is False
    assert state["reason"] == "missing"


def test_get_settings_blocks_core_keys_without_grant(tmp_path):
    """An extension that lists a core key in env_from_settings without
    an owner grant fails to load and ``PluginAPIImpl.get_settings``
    silently drops the key — the dual-track grant model deliberately
    keeps the failure mode the same as the script path."""
    plugin = (
        "def register(api):\n"
        "    api.register_tool('n', lambda ctx: 'ok', description='n', schema={})\n"
    )
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "envtest",
        plugin,
        permissions=["tool", "read_settings"],
        env_from_settings=["OPENROUTER_API_KEY", "TIMEZONE", "MY_OK"],
    )
    settings_snapshot = {
        "OPENROUTER_API_KEY": "sk-leak",
        "TIMEZONE": "UTC",
        "MY_OK": "visible",
        "RANDOM_OTHER": "not-allowed",
    }
    err = extension_loader.load_extension(loaded, lambda: settings_snapshot, drive_root=drive_root)
    assert err is not None
    assert "missing owner grants" in err
    assert "OPENROUTER_API_KEY" in err

    impl = extension_loader.PluginAPIImpl(
        skill_name="envtest",
        permissions=["read_settings"],
        env_allowlist=["OPENROUTER_API_KEY", "TIMEZONE", "MY_OK"],
        state_dir=tmp_path,
        settings_reader=lambda: settings_snapshot,
        granted_keys=[],
    )
    got = impl.get_settings(["OPENROUTER_API_KEY", "TIMEZONE", "MY_OK", "RANDOM_OTHER"])
    assert "OPENROUTER_API_KEY" not in got
    assert got["TIMEZONE"] == "UTC"
    assert got["MY_OK"] == "visible"
    assert "RANDOM_OTHER" not in got
    impl._close_runtime_access()
    assert impl.get_settings(["TIMEZONE", "MY_OK"]) == {}


def test_get_settings_rechecks_runtime_close_after_reader_returns(tmp_path):
    import threading

    reader_started = threading.Event()
    release_reader = threading.Event()

    def settings_reader():
        reader_started.set()
        assert release_reader.wait(1.0)
        return {"MY_OK": "visible"}

    impl = extension_loader.PluginAPIImpl(
        skill_name="settings_race",
        permissions=["read_settings"],
        env_allowlist=["MY_OK"],
        state_dir=tmp_path,
        settings_reader=settings_reader,
    )
    result = []
    thread = threading.Thread(target=lambda: result.append(impl.get_settings(["MY_OK"])))
    thread.start()
    assert reader_started.wait(1.0)
    close_done = threading.Event()
    close_thread = threading.Thread(target=lambda: (impl._close_runtime_access(), close_done.set()))
    close_thread.start()
    assert not close_done.wait(0.1)
    release_reader.set()
    thread.join(timeout=1.0)
    close_thread.join(timeout=1.0)

    assert close_done.is_set()
    assert result == [{}]
    assert impl.get_settings(["MY_OK"]) == {}


def test_unload_does_not_deadlock_with_inflight_get_settings(tmp_path):
    import threading
    import time

    reader_started = threading.Event()
    release_reader = threading.Event()

    def settings_reader():
        reader_started.set()
        release_reader.wait()
        return {"MY_OK": "visible"}

    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "settings_unload_race",
        "import threading\n"
        "def register(api):\n"
        "    threading.Thread(target=lambda: api.get_settings(['MY_OK'])).start()\n"
        "    api.register_tool('ping', lambda **kw: 'pong', description='ping', schema={})\n",
        permissions=["tool", "read_settings"],
        env_from_settings=["MY_OK"],
    )
    err = extension_loader.load_extension(loaded, settings_reader, drive_root=drive_root)
    assert err is None, err
    assert reader_started.wait(1.0)

    unload_done = threading.Event()
    unload_thread = threading.Thread(target=lambda: (extension_loader.unload_extension("settings_unload_race"), unload_done.set()))
    unload_thread.start()
    time.sleep(0.1)
    release_reader.set()
    unload_thread.join(timeout=1.0)

    assert unload_done.is_set()
    assert extension_loader.snapshot()["extensions"] == []


def test_load_extension_rejects_grant_with_stale_content_hash(tmp_path):
    """v5.2.2 dual-track grants: the loader binds the persisted grant
    to the current content hash. A grants.json written for a prior
    revision must NOT authorise the freshly-edited plugin (defense in
    depth — even if ``grant_status_for_skill`` is bypassed)."""
    from ouroboros.skill_loader import save_skill_grants

    plugin = (
        "def register(api):\n"
        "    api.register_tool('n', lambda ctx: 'ok', description='n', schema={})\n"
    )
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "stale_grant",
        plugin,
        permissions=["tool", "read_settings"],
        env_from_settings=["OPENROUTER_API_KEY"],
    )
    # Persist a grant with the WRONG content hash — simulates a manifest
    # / plugin edit that the operator has not re-authorised.
    save_skill_grants(
        drive_root,
        "stale_grant",
        ["OPENROUTER_API_KEY"],
        content_hash="some-other-hash",
        requested_keys=["OPENROUTER_API_KEY"],
    )
    err = extension_loader.load_extension(
        loaded,
        lambda: {"OPENROUTER_API_KEY": "sk-secret"},
        drive_root=drive_root,
    )
    assert err is not None
    assert "missing owner grants" in err


def test_get_settings_returns_core_key_with_grant(tmp_path):
    """An owner-granted core key is forwarded to the in-process plugin
    via ``PluginAPIImpl.get_settings``. The grant must be bound to the
    current content hash + manifest-requested set; ``load_extension``
    enforces both before constructing the API impl."""
    from ouroboros.skill_loader import save_skill_grants

    plugin = (
        "def register(api):\n"
        "    api.register_tool('n', lambda ctx: 'ok', description='n', schema={})\n"
    )
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "granted_ext",
        plugin,
        permissions=["tool", "read_settings"],
        env_from_settings=["OPENROUTER_API_KEY", "TIMEZONE"],
    )
    save_skill_grants(
        drive_root,
        "granted_ext",
        ["OPENROUTER_API_KEY"],
        content_hash=loaded.content_hash,
        requested_keys=["OPENROUTER_API_KEY"],
    )
    settings_snapshot = {
        "OPENROUTER_API_KEY": "sk-allowed",
        "TIMEZONE": "UTC",
    }
    err = extension_loader.load_extension(loaded, lambda: settings_snapshot, drive_root=drive_root)
    assert err is None, err

    impl = extension_loader.PluginAPIImpl(
        skill_name="granted_ext",
        permissions=["read_settings"],
        env_allowlist=["OPENROUTER_API_KEY", "TIMEZONE"],
        state_dir=tmp_path,
        settings_reader=lambda: settings_snapshot,
        granted_keys=["OPENROUTER_API_KEY"],
    )
    got = impl.get_settings(["OPENROUTER_API_KEY", "TIMEZONE"])
    assert got.get("OPENROUTER_API_KEY") == "sk-allowed"
    assert got.get("TIMEZONE") == "UTC"

    # Grant on the WRONG content hash must not authorise — the loader
    # builds an empty granted_keys list and drops the value.
    impl_no_grant = extension_loader.PluginAPIImpl(
        skill_name="granted_ext",
        permissions=["read_settings"],
        env_allowlist=["OPENROUTER_API_KEY", "TIMEZONE"],
        state_dir=tmp_path,
        settings_reader=lambda: settings_snapshot,
        granted_keys=[],
    )
    assert "OPENROUTER_API_KEY" not in impl_no_grant.get_settings(["OPENROUTER_API_KEY"])


def test_unload_removes_all_registrations(tmp_path):
    plugin = (
        "def _t(c): return 'x'\n"
        "def _r(req): return {}\n"
        "def _w(p): return {}\n"
        "def register(api):\n"
        "    api.register_tool('t', _t, description='', schema={})\n"
        "    api.register_route('r', _r)\n"
        "    api.register_ws_handler('w', _w)\n"
    )
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "full",
        plugin,
        permissions=["tool", "route", "ws_handler"],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    snap = extension_loader.snapshot()
    assert snap["tools"] and snap["routes"] and snap["ws_handlers"]

    extension_loader.unload_extension("full")
    snap = extension_loader.snapshot()
    assert snap["tools"] == []
    assert snap["routes"] == []
    assert snap["ws_handlers"] == []
    assert snap["extensions"] == []


def test_reload_all_called_on_settings_save():
    """Phase 4 regression: ``server.py::api_settings_post`` must
    reconcile the live extension registry when OUROBOROS_SKILLS_REPO_PATH
    changes; otherwise switching repo path leaves stale extensions
    registered from the old path."""
    import ast
    src = (
        pathlib.Path(__file__).resolve().parent.parent
        / "ouroboros"
        / "gateway"
        / "settings.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "api_settings_post"
        ):
            body_text = ast.unparse(node)
            assert "reload_all" in body_text or "_reload_extensions" in body_text, (
                "api_settings_post must call extension_loader.reload_all on "
                "OUROBOROS_SKILLS_REPO_PATH change."
            )
            assert "OUROBOROS_SKILLS_REPO_PATH" in body_text
            assert "OUROBOROS_RUNTIME_MODE" in body_text, (
                "api_settings_post must also reconcile extensions when "
                "runtime mode changes."
            )
            return
    assert False, "api_settings_post function not found in gateway/settings.py"


def test_reload_all_called_from_server_startup():
    """Phase 4 regression: server.py main() must call
    ``extension_loader.reload_all`` during startup so enabled extensions
    survive a restart. Without this, only ``toggle_skill`` could ever
    load a plugin."""
    import ast
    src = (pathlib.Path(__file__).resolve().parent.parent / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan":
            body_text = ast.unparse(node)
            assert "_reload_extensions(" in body_text or "reload_all(" in body_text, (
                "server.py does not wire extension_loader.reload_all into startup — "
                "enabled extensions would not survive a process restart."
            )
            assert "if repo_path" not in body_text, (
                "startup extension reload must run even when only bundled "
                "skills are present."
            )
            assert "pytest_default_real_data_dir" in body_text
            assert "Skipping extension reload_all against real DATA_DIR during pytest" in body_text
            return
    assert False, "lifespan function not found in server.py"


def test_reload_all_tears_down_stale_extensions(tmp_path):
    """reload_all must unload extensions that no longer exist on disk."""
    plugin = (
        "def register(api):\n"
        "    api.register_tool('t', lambda ctx: 'ok', description='', schema={})\n"
    )
    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "staleish",
        plugin,
        permissions=["tool"],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None
    assert "staleish" in extension_loader.snapshot()["extensions"]
    # Nuke the skill directory; reload_all should tear it down.
    import shutil
    shutil.rmtree(repo_root / "staleish")
    extension_loader.reload_all(drive_root, lambda: {}, repo_path=str(repo_root))
    assert "staleish" not in extension_loader.snapshot()["extensions"]


def test_reload_all_continues_after_one_extension_exception(tmp_path, monkeypatch, caplog):
    """A reconcile bug in one extension must not block later extensions."""
    import logging

    repo_root = tmp_path / "skills"
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    plugin = (
        "def register(api):\n"
        "    api.register_tool('t', lambda ctx: 'ok', description='', schema={})\n"
    )
    for name in ("a_bad", "z_good"):
        _write_ext_skill(repo_root, name, plugin_body=plugin, permissions=["tool"])
        loaded = find_skill(drive_root, name, repo_path=str(repo_root))
        assert loaded is not None
        save_enabled(drive_root, name, True)
        save_review_state(drive_root, name, SkillReviewState(status="pass", content_hash=loaded.content_hash))

    original_reconcile = extension_loader.reconcile_extension

    def flaky_reconcile(skill_name, *args, **kwargs):
        if skill_name == "a_bad":
            raise RuntimeError("boom")
        return original_reconcile(skill_name, *args, **kwargs)

    monkeypatch.setattr(extension_loader, "reconcile_extension", flaky_reconcile)

    with caplog.at_level(logging.ERROR):
        results = extension_loader.reload_all(drive_root, lambda: {}, repo_path=str(repo_root))

    assert "RuntimeError: boom" in results["a_bad"]
    assert results["z_good"] is None
    assert "z_good" in extension_loader.snapshot()["extensions"]
    assert any("Extension reload failed for a_bad; continuing" in rec.message for rec in caplog.records)


def test_reload_all_logs_per_extension_load_error(tmp_path, caplog):
    import logging

    repo_root = tmp_path / "skills"
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    _write_ext_skill(
        repo_root,
        "bad_register",
        plugin_body="def register(api):\n    raise RuntimeError('register failed')\n",
        permissions=[],
    )
    loaded = find_skill(drive_root, "bad_register", repo_path=str(repo_root))
    assert loaded is not None
    save_enabled(drive_root, "bad_register", True)
    save_review_state(drive_root, "bad_register", SkillReviewState(status="pass", content_hash=loaded.content_hash))

    with caplog.at_level(logging.ERROR):
        results = extension_loader.reload_all(drive_root, lambda: {}, repo_path=str(repo_root))

    assert "register failed" in str(results["bad_register"])
    assert any("Extension reload failed for bad_register" in rec.message for rec in caplog.records)


def test_unload_clears_child_module_cache(tmp_path):
    """Phase 4 round 3 regression: unload must purge EVERY
    ``ouroboros._extensions.<skill>.*`` entry from sys.modules, not
    just the top-level module. Otherwise a helper-file edit sticks to
    the stale cached module on reload."""
    import sys as _sys
    skill_dir = tmp_path / "skills" / "tree_ext"
    (skill_dir).mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            "name: tree_ext\n"
            "description: Multi-file extension.\n"
            "version: 0.1.0\n"
            "type: extension\n"
            "entry: plugin.py\n"
            "permissions: [\"tool\"]\n"
            "env_from_settings: []\n"
            "---\n"
            "body\n"
        ),
        encoding="utf-8",
    )
    (skill_dir / "helper.py").write_text("X = 'v1'\n", encoding="utf-8")
    (skill_dir / "plugin.py").write_text(
        (
            "from .helper import X\n"
            "def _t(ctx): return X\n"
            "def register(api):\n"
            "    api.register_tool('echo', _t, description='', schema={})\n"
        ),
        encoding="utf-8",
    )
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    from ouroboros.skill_loader import find_skill
    save_enabled(drive_root, "tree_ext", True)
    loaded = find_skill(drive_root, "tree_ext", repo_path=str(skill_dir.parent))
    assert loaded is not None
    save_review_state(
        drive_root,
        "tree_ext",
        SkillReviewState(status="pass", content_hash=loaded.content_hash),
    )
    loaded = find_skill(drive_root, "tree_ext", repo_path=str(skill_dir.parent))

    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    # Both the package module and its helper child module must live in
    # sys.modules after import, and BOTH must be purged on unload.
    parent_key = extension_loader._module_key("tree_ext")
    child_key = f"{parent_key}.helper"
    assert parent_key in _sys.modules
    assert child_key in _sys.modules
    extension_loader.unload_extension("tree_ext")
    assert parent_key not in _sys.modules
    assert child_key not in _sys.modules


def test_load_extension_requires_explicit_drive_root(tmp_path):
    loaded, _repo_root, _drive_root = _prepare_extension(
        tmp_path,
        "requires_root",
        "def register(api):\n    pass\n",
        [],
    )

    with pytest.raises(TypeError):
        extension_loader.load_extension(loaded, lambda: {})


def test_clean_extension_runtime_state_unloads_staged_import_root(tmp_path):
    loaded, _repo_root, drive_root = _prepare_extension(
        tmp_path,
        "cleanup_ext",
        "def register(api):\n    pass\n",
        [],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    with extension_loader._lock:
        import_root = pathlib.Path(extension_loader._extensions["cleanup_ext"].import_root)
    assert import_root.exists()

    clean_extension_runtime_state()

    assert not import_root.exists()
    assert "cleanup_ext" not in extension_loader.snapshot()["extensions"]


def test_reload_all_sweeps_stale_extension_imports(tmp_path, monkeypatch):
    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "sweep_ext",
        "def register(api):\n    pass\n",
        [],
    )
    stale_root = drive_root / "state" / "skills" / "sweep_ext" / "__extension_imports" / "stale"
    (stale_root / "skill").mkdir(parents=True)
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(repo_root))

    results = extension_loader.reload_all(drive_root, lambda: {}, repo_path=str(repo_root))

    assert results["sweep_ext"] is None
    assert not stale_root.exists()
    live_roots = list((drive_root / "state" / "skills" / "sweep_ext" / "__extension_imports").iterdir())
    assert len(live_roots) == 1
    assert (live_roots[0] / "skill").exists()


def test_reload_all_preserves_live_import_root_while_sweeping_stale_roots(tmp_path, monkeypatch):
    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "live_sweep_ext",
        "def register(api):\n    pass\n",
        [],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    with extension_loader._lock:
        live_root = pathlib.Path(extension_loader._extensions["live_sweep_ext"].import_root)
    stale_root = drive_root / "state" / "skills" / "live_sweep_ext" / "__extension_imports" / "stale"
    (stale_root / "skill").mkdir(parents=True)
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(repo_root))

    results = extension_loader.reload_all(drive_root, lambda: {}, repo_path=str(repo_root))

    assert results["live_sweep_ext"] is None
    assert live_root.exists()
    assert not stale_root.exists()
    live_roots = list((drive_root / "state" / "skills" / "live_sweep_ext" / "__extension_imports").iterdir())
    assert live_roots == [live_root]


def test_tool_registration_collision_raises(tmp_path):
    """Two plugins registering the same tool namespace collide."""
    plugin_a = (
        "def register(api):\n"
        "    api.register_tool('same', lambda ctx: 'a', description='', schema={})\n"
        "    api.register_tool('same', lambda ctx: 'b', description='', schema={})\n"
    )
    loaded, _, drive_root = _prepare_extension(tmp_path, "collider", plugin_a, permissions=["tool"])
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is not None
    assert "already registered" in err
    # Collision raised mid-registration must tear down the first tool too.
    assert extension_loader.snapshot()["tools"] == []
