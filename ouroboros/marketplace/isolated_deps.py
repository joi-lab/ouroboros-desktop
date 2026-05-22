"""Per-skill isolated dependency installation helpers."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
from typing import Any, Dict, List

from ouroboros.marketplace.install_specs import install_specs_hash
from ouroboros.skill_loader import skill_state_dir
from ouroboros.utils import atomic_write_json, read_json_dict, utc_now_iso


ENV_DIRNAME = ".ouroboros_env"
FINGERPRINT_FILENAME = "fingerprint.json"
DEPS_STATE_FILENAME = "deps.json"
_DEFAULT_TIMEOUT_SEC = 600
_SAFE_ENV_KEYS = {"PATH", "SYSTEMROOT", "LANG", "LC_ALL", "LC_CTYPE"}


def isolated_env_dir(skill_dir: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(skill_dir) / ENV_DIRNAME


def isolated_bin_dirs(skill_dir: pathlib.Path) -> List[pathlib.Path]:
    env_root = isolated_env_dir(skill_dir)
    candidates = [
        env_root / "bin",
        env_root / "python" / ("Scripts" if os.name == "nt" else "bin"),
        env_root / "node" / "node_modules" / ".bin",
        env_root / "cargo" / "bin",
    ]
    return [path for path in candidates if path.exists()]


def python_runtime_binary(skill_dir: pathlib.Path) -> pathlib.Path | None:
    bin_dir = isolated_env_dir(skill_dir) / "python" / ("Scripts" if os.name == "nt" else "bin")
    candidate = bin_dir / ("python.exe" if os.name == "nt" else "python")
    return candidate if candidate.is_file() else None


def augment_env_for_skill_deps(env: Dict[str, str], skill_dir: pathlib.Path) -> Dict[str, str]:
    out = dict(env)
    env_root = isolated_env_dir(skill_dir)
    bins = [str(path) for path in isolated_bin_dirs(skill_dir)]
    if bins:
        current = out.get("PATH", "")
        out["PATH"] = os.pathsep.join([*bins, current]) if current else os.pathsep.join(bins)
    python_bin = python_runtime_binary(skill_dir)
    if python_bin:
        out["VIRTUAL_ENV"] = str(python_bin.parent.parent)
    node_modules = env_root / "node" / "node_modules"
    if node_modules.is_dir():
        out["NODE_PATH"] = str(node_modules)
    return out


def _installer_env(env_root: pathlib.Path, *, ecosystem: str = "") -> Dict[str, str]:
    tmp_dir = env_root / "tmp"
    home_dir = env_root / "home"
    cache_dir = env_root / "cache"
    for path in (tmp_dir, home_dir, cache_dir):
        path.mkdir(parents=True, exist_ok=True)
    env = {key: os.environ[key] for key in _SAFE_ENV_KEYS if key in os.environ}
    env.update({
        "HOME": str(home_dir), "USERPROFILE": str(home_dir),
        "APPDATA": str(home_dir / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home_dir / "AppData" / "Local"),
        "TMPDIR": str(tmp_dir), "TMP": str(tmp_dir), "TEMP": str(tmp_dir),
        "PYTHONNOUSERSITE": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_CACHE_DIR": str(cache_dir / "pip"), "PIP_CONFIG_FILE": os.devnull,
        "npm_config_cache": str(cache_dir / "npm"), "npm_config_userconfig": str(env_root / "npmrc"),
        "CARGO_HOME": str(env_root / "cargo" / "home"), "CARGO_TARGET_DIR": str(env_root / "cargo" / "target"),
    })
    return env


def _run(cmd: List[str], *, cwd: pathlib.Path, env: Dict[str, str], timeout_sec: int) -> Dict[str, Any]:
    from subprocess import Popen
    from ouroboros.platform_layer import merge_hidden_kwargs, subprocess_new_group_kwargs
    from ouroboros.tools.shell import _active_subprocesses, _kill_process_group, _subprocess_lock

    kwargs: Dict[str, Any] = {
        "cwd": str(cwd),
        "env": env,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    kwargs.update(subprocess_new_group_kwargs())
    proc = Popen(cmd, **merge_hidden_kwargs(kwargs))  # noqa: S603 - argv template is controlled.
    with _subprocess_lock:
        _active_subprocesses.add(proc)
    try:
        proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        raise
    finally:
        with _subprocess_lock:
            _active_subprocesses.discard(proc)
    return {"cmd": cmd[:2] + ["..."] if len(cmd) > 2 else list(cmd), "returncode": proc.returncode}


def _ensure_python_env(env_root: pathlib.Path, timeout_sec: int) -> pathlib.Path:
    venv_dir = env_root / "python"
    if not venv_dir.exists():
        result = _run([sys.executable, "-m", "venv", str(venv_dir)], cwd=env_root, env=_installer_env(env_root, ecosystem="python"), timeout_sec=timeout_sec)
        if result["returncode"] != 0:
            raise RuntimeError("python venv creation failed")
    return venv_dir / ("Scripts" if os.name == "nt" else "bin")


def _install_python_packages(packages: List[str], env_root: pathlib.Path, timeout_sec: int) -> List[Dict[str, Any]]:
    if not packages:
        return []
    bin_dir = _ensure_python_env(env_root, timeout_sec)
    python_bin = bin_dir / ("python.exe" if os.name == "nt" else "python")
    result = _run([str(python_bin), "-m", "pip", "install", "--only-binary=:all:", *packages], cwd=env_root, env=_installer_env(env_root, ecosystem="python"), timeout_sec=timeout_sec)
    if result["returncode"] != 0:
        raise RuntimeError("pip install failed")
    return [result]


def _install_node_package(package: str, env_root: pathlib.Path, timeout_sec: int) -> List[Dict[str, Any]]:
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("npm is not available on PATH")
    node_root = env_root / "node"
    node_root.mkdir(parents=True, exist_ok=True)
    env = _installer_env(env_root, ecosystem="node")
    env["npm_config_prefix"] = str(node_root)
    result = _run([npm, "install", "--ignore-scripts", "--prefix", str(node_root), package], cwd=env_root, env=env, timeout_sec=timeout_sec)
    if result["returncode"] != 0:
        raise RuntimeError(f"npm install {package!r} failed")
    skill_node_modules = env_root.parent / "node_modules"
    target_node_modules = node_root / "node_modules"
    if target_node_modules.exists() and not skill_node_modules.exists():
        try:
            skill_node_modules.symlink_to(target_node_modules, target_is_directory=True)
        except OSError:
            # Some Windows configurations disallow symlinks. PATH + NODE_PATH
            # still cover CommonJS and CLI binaries; ESM import users will see
            # a normal module-resolution error instead of a privileged fallback.
            pass
    return [result]


def install_isolated_dependencies(
    drive_root: pathlib.Path,
    skill_name: str,
    skill_dir: pathlib.Path,
    specs: List[Dict[str, Any]],
    *,
    timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
) -> Dict[str, Any]:
    """Install normalized specs and persist deps.json status/specs_hash."""

    env_root = isolated_env_dir(skill_dir)
    env_root.mkdir(parents=True, exist_ok=True)
    installed: List[Dict[str, Any]] = []
    logs: List[Dict[str, Any]] = []
    python_packages: List[str] = []
    failure: Dict[str, Any] = {}
    try:
        for spec in specs:
            kind = str(spec.get("kind") or "").lower()
            package = str(spec.get("package") or "").strip()
            if kind in {"pip", "pipx", "uv"}:
                python_packages.append(package)
            elif kind in {"node", "npm"}:
                logs.extend(_install_node_package(package, env_root, timeout_sec))
            elif kind == "cargo":
                raise RuntimeError("cargo install specs require manual setup")
            else:
                raise RuntimeError(f"unsupported isolated install kind: {kind}")
            installed.append({"kind": kind, "package": package, "bins": list(spec.get("bins") or [])})
        if python_packages:
            logs.extend(_install_python_packages(python_packages, env_root, timeout_sec))
    except Exception as exc:
        failure = {"error": f"{type(exc).__name__}: {exc}"}
    fingerprint = {
        "schema_version": 1,
        "installed_at": utc_now_iso(),
        "skill": skill_name,
        "env_dir": ENV_DIRNAME,
        "specs_hash": install_specs_hash(specs),
        "installed": installed,
        "logs": logs[-10:],
        "status": "failed" if failure else "installed",
        "error": failure.get("error", ""),
    }
    atomic_write_json(env_root / FINGERPRINT_FILENAME, fingerprint, trailing_newline=True)
    state_dir = skill_state_dir(drive_root, skill_name)
    atomic_write_json(state_dir / DEPS_STATE_FILENAME, fingerprint, trailing_newline=True)
    if failure:
        # Re-raise after durable failed fingerprint is written.
        raise RuntimeError(failure["error"])
    return fingerprint


def read_deps_state(
    drive_root: pathlib.Path,
    skill_name: str,
    skill_dir: pathlib.Path | None = None,
) -> Dict[str, Any]:
    """Return persisted deps.json, optionally verified against the live env."""
    try:
        state_dir = skill_state_dir(drive_root, skill_name)
        path = state_dir / DEPS_STATE_FILENAME
        state = read_json_dict(path) or {}
    except Exception:
        return {}
    if skill_dir is None or str(state.get("status") or "") != "installed":
        return state
    fingerprint = read_json_dict(isolated_env_dir(skill_dir) / FINGERPRINT_FILENAME) or {}
    state_hash = str(state.get("specs_hash") or "")
    fingerprint_hash = str(fingerprint.get("specs_hash") or "")
    if str(fingerprint.get("status") or "") != "installed":
        return {**state, "status": "missing", "error": "isolated environment fingerprint is missing"}
    if fingerprint_hash != state_hash:
        return {**state, "status": "stale", "error": "isolated environment fingerprint is stale"}
    return state
