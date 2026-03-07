# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Ouroboros (macOS, Linux, Windows).

Bundles launcher.py as the entry point. The agent code (server.py, ouroboros/,
supervisor/, web/) is included as data and copied to ~/Ouroboros/repo/ on first run.
The embedded python-standalone interpreter runs the agent as a subprocess.
"""

import os
import sys

block_cipher = None

# ---------------------------------------------------------------------------
# Platform-specific settings
# ---------------------------------------------------------------------------
_is_macos = sys.platform == "darwin"
_is_windows = sys.platform == "win32"

if _is_windows:
    _icon = 'assets/icon.ico' if os.path.exists('assets/icon.ico') else None
    _console = False
elif _is_macos:
    _icon = 'assets/icon.icns'
    _console = False
else:
    _icon = None
    _console = False

# ---------------------------------------------------------------------------
# Strip dev-only files from python-standalone before bundling.
# python-build-standalone ships symlinks (lib/pkgconfig, etc.) that break
# PyInstaller's BUNDLE step on macOS.
# ---------------------------------------------------------------------------
import shutil as _shutil
for _sub in ('include', 'share', 'lib/pkgconfig'):
    _p = os.path.join('python-standalone', _sub)
    if os.path.islink(_p):
        os.remove(_p)
    elif os.path.isdir(_p):
        _shutil.rmtree(_p)

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('VERSION', '.'),
        ('.gitignore', '.'),
        ('BIBLE.md', '.'),
        ('README.md', '.'),
        ('requirements.txt', '.'),
        ('requirements-launcher.txt', '.'),
        ('pyproject.toml', '.'),
        ('Makefile', '.'),
        ('server.py', '.'),
        ('ouroboros', 'ouroboros'),
        ('supervisor', 'supervisor'),
        ('prompts', 'prompts'),
        ('web', 'web'),
        ('tests', 'tests'),
        ('assets/logo.jpg', 'assets'),
        ('python-standalone', 'python-standalone'),
    ],
    hiddenimports=[
        'webview',
        'ouroboros.config',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Ouroboros',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=_console,
    disable_windowed_traceback=False,
    icon=_icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Ouroboros',
)

# macOS application bundle (skipped on Linux/Windows)
if _is_macos:
    app = BUNDLE(
        coll,
        name='Ouroboros.app',
        icon='assets/icon.icns',
        bundle_identifier='com.ouroboros.agent',
        info_plist={
            'CFBundleShortVersionString': open('VERSION').read().strip(),
            'CFBundleVersion': open('VERSION').read().strip(),
            'NSHighResolutionCapable': True,
            'LSMinimumSystemVersion': '12.0',
        },
    )
