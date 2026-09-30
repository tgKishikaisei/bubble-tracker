# -*- mode: python ; coding: utf-8 -*-
# bubble_tracker.spec — сборка portable .exe для Bubble Tracker PRO

import sys
from pathlib import Path

block_cipher = None

# Папка назначения — PORTABLE папка
PORTABLE_DIR = str(Path('.') / 'BubbleTracker_PORTABLE')

a = Analysis(
    ['bubble_tracker_pro.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=[
        'cv2',
        'numpy',
        'numpy.core._multiarray_umath',
        'numpy.core._multiarray_methods',
        'openpyxl',
        'openpyxl.styles',
        'openpyxl.styles.fonts',
        'openpyxl.cell',
        'tkinter',
        'tkinter.ttk',
        'tkinter.filedialog',
        'tkinter.messagebox',
        'scipy',
        'scipy.ndimage',
        'skimage',
        'skimage.morphology',
        'skimage.feature',
        'skimage.segmentation',
        'skimage.measure',
        'PIL',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'IPython',
        'pandas', 'PyQt5', 'PyQt6', 'wx',
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
    [],
    exclude_binaries=True,
    name='BubbleTrackerPRO',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # True = показывает консоль с логами
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='BubbleTrackerPRO',
    distpath=PORTABLE_DIR,
)
