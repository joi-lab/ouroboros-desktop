"""PyInstaller runtime hook: configure pythonnet for frozen bundles.

Must run before any ``import clr`` / ``import webview`` so that
Python.Runtime.dll can locate the bundled pythonXY.dll.
"""
import os
import sys

if sys.platform == "win32":
    _base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    _pydll = os.path.join(
        _base, f"python{sys.version_info[0]}{sys.version_info[1]}.dll"
    )
    if os.path.exists(_pydll):
        os.environ.setdefault("PYTHONNET_PYDLL", _pydll)
