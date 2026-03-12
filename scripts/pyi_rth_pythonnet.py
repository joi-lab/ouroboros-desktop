"""PyInstaller runtime hook: configure pythonnet for frozen bundles.

Must run before any ``import clr`` / ``import webview`` so that
Python.Runtime.dll can locate the bundled pythonXY.dll.
"""
import os
import sys

if sys.platform == "win32":
    _base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    _exe_dir = os.path.dirname(sys.executable)
    _dll_name = f"python{sys.version_info[0]}{sys.version_info[1]}.dll"
    _runtime_dir = os.path.join(_base, "pythonnet", "runtime")
    _webview_lib_dir = os.path.join(_base, "webview", "lib")

    os.environ["PYTHONNET_RUNTIME"] = "netfx"

    _candidates = [
        os.path.join(_base, _dll_name),
        os.path.join(_exe_dir, _dll_name),
    ]
    if _base != _exe_dir:
        for _root, _dirs, _files in os.walk(_base):
            if _dll_name in _files:
                _candidates.append(os.path.join(_root, _dll_name))
            if len(_candidates) > 5:
                break

    def _unblock_file(_path: str) -> None:
        try:
            os.remove(f"{_path}:Zone.Identifier")
        except OSError:
            pass

    def _unblock_tree(_root: str) -> None:
        if not os.path.isdir(_root):
            return
        for _dirpath, _dirnames, _filenames in os.walk(_root):
            for _filename in _filenames:
                if os.path.splitext(_filename)[1].lower() in {".dll", ".exe", ".pyd"}:
                    _unblock_file(os.path.join(_dirpath, _filename))

    _selected_pydll = None
    for _path in _candidates:
        if os.path.isfile(_path):
            os.environ["PYTHONNET_PYDLL"] = _path
            _unblock_file(_path)
            _selected_pydll = _path
            break

    _runtime_dll = os.path.join(_runtime_dir, "Python.Runtime.dll")
    if not os.path.isfile(_runtime_dll):
        for _root, _dirs, _files in os.walk(_base):
            if "Python.Runtime.dll" in _files:
                _runtime_dll = os.path.join(_root, "Python.Runtime.dll")
                break
    if os.path.isfile(_runtime_dll):
        _unblock_file(_runtime_dll)
        _unblock_tree(os.path.dirname(_runtime_dll))
    _unblock_tree(_webview_lib_dir)

    _search_dirs = []
    for _path in (
        _base,
        _exe_dir,
        _runtime_dir,
        os.path.dirname(_runtime_dll) if os.path.isfile(_runtime_dll) else None,
        os.path.dirname(_selected_pydll) if _selected_pydll else None,
        _webview_lib_dir,
    ):
        if os.path.isdir(_path) and _path not in _search_dirs:
            _search_dirs.append(_path)
    os.environ["PATH"] = os.pathsep.join(
        _search_dirs + [p for p in os.environ.get("PATH", "").split(os.pathsep) if p and p not in _search_dirs]
    )

    if hasattr(os, "add_dll_directory"):
        _dll_dir_handles = []
        for _path in _search_dirs:
            try:
                _dll_dir_handles.append(os.add_dll_directory(_path))
            except (FileNotFoundError, OSError):
                pass
