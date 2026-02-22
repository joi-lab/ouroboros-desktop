# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Ouroboros.app (macOS).

Bundles launcher.py as the entry point. The agent code (server.py, ouroboros/,
supervisor/, web/) is included as data and copied to ~/Ouroboros/repo/ on first run.
The embedded python-standalone interpreter runs the agent as a subprocess.
"""

import os
import sys

block_cipher = None

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('VERSION', '.'),
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
    console=False,
    disable_windowed_traceback=False,
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
