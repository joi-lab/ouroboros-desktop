# -*- mode: python ; coding: utf-8 -*-

import pathlib
_version = pathlib.Path('VERSION').read_text(encoding='utf-8').strip()

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('ouroboros', 'ouroboros'),
        ('supervisor', 'supervisor'),
        ('ui', 'ui'),
        ('prompts', 'prompts'),
        ('BIBLE.md', '.'),
        ('VERSION', '.'),
        ('README.md', '.'),
        ('pyproject.toml', '.'),
        ('requirements.txt', '.'),
    ],
    hiddenimports=[
        # Tool modules (auto-discovered via pkgutil at runtime)
        'ouroboros.tools.browser',
        'ouroboros.tools.compact_context',
        'ouroboros.tools.control',
        'ouroboros.tools.core',
        'ouroboros.tools.evolution_stats',
        'ouroboros.tools.git',
        'ouroboros.tools.github',
        'ouroboros.tools.health',
        'ouroboros.tools.knowledge',
        'ouroboros.tools.review',
        'ouroboros.tools.search',
        'ouroboros.tools.shell',
        'ouroboros.tools.tool_discovery',
        'ouroboros.tools.vision',
        # Core modules
        'ouroboros.agent',
        'ouroboros.consciousness',
        'ouroboros.context',
        'ouroboros.llm',
        'ouroboros.loop',
        'ouroboros.memory',
        'ouroboros.review',
        'ouroboros.safety',
        'ouroboros.owner_inject',
        'ouroboros.apply_patch',
        'ouroboros.world_profiler',
        # Supervisor modules
        'supervisor.events',
        'supervisor.git_ops',
        'supervisor.queue',
        'supervisor.state',
        'supervisor.telegram',
        'supervisor.workers',
        # UI modules
        'ui.components',
        'ui.log_format',
        'ui.notifications',
        'ui.first_run',
        # Third-party that PyInstaller may miss
        'dulwich',
        'dulwich.repo',
        'dulwich.objects',
        'dulwich.pack',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'playwright',
        'playwright_stealth',
        'colab_launcher',
        'colab_bootstrap_shim',
        'tkinter',
        'matplotlib',
        'numpy',
        'scipy',
        'pandas',
        'IPython',
        'notebook',
        'jupyter',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Ouroboros',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

app = BUNDLE(
    exe,
    name='Ouroboros.app',
    icon='assets/icon.icns',
    bundle_identifier='com.ouroboros.agent',
    info_plist={
        'CFBundleShortVersionString': _version,
        'NSHighResolutionCapable': True,
    },
)
