# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec file for SeriWrap
# Usage: pyinstaller seriwrap.spec

block_cipher = None

a = Analysis(
    ['ip_main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('templates', 'templates'),  # Include Jinja2 templates
    ],
    hiddenimports=[
        'jinja2',
        'jinja2.runtime',
        'bram_generator',
        'pll_generator',
        'stream_generator',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude unnecessary modules to reduce size
        'matplotlib',
        'numpy',
        'pandas',
        'scipy',
        'tkinter',
        'unittest',
        'pytest',
        'pydoc',
        'email',
        'html',
        'http',
        'xml',
        'xmlrpc',
        'ssl',
        'multiprocessing',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='seriwrap',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,  # Enable UPX compression
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # Keep console for JSON output
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon='icon.ico',  # Add icon if available
)
