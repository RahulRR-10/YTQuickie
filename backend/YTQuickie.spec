# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the YTQuickie desktop app.
# Run from the repo root:  pyinstaller backend/YTQuickie.spec --noconfirm
import os
from PyInstaller.utils.hooks import collect_submodules

HERE = os.path.dirname(os.path.abspath(SPEC))
ROOT = os.path.dirname(HERE)

datas = [
    # Built React frontend (frontend/ -> ../backend/static)
    (os.path.join(HERE, "static"), "static"),
    # Bundled FFmpeg binaries (project root/release/ffmpeg)
    (os.path.join(ROOT, "release", "ffmpeg"), "ffmpeg"),
]

# Bundled Node.js runtime for YouTube's JS-challenge solver (yt-dlp EJS).
# Drop a portable node.exe (e.g. from https://nodejs.org win-x64 zip) into
# release/nodejs/ before running PyInstaller; it is picked up automatically
# by backend/main.py `_find_node_binary()` via sys._MEIPASS/nodejs.
_NODEJS_SRC = os.path.join(ROOT, "release", "nodejs")
if os.path.isdir(_NODEJS_SRC):
    datas.append((_NODEJS_SRC, "nodejs"))

hiddenimports = [
    "webview.platforms.winforms",
    "clr",
    "clr_loader",
    "sse_starlette",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
]
# yt-dlp loads extractors dynamically
hiddenimports += collect_submodules("yt_dlp")

a = Analysis(
    [os.path.join(HERE, "desktop.py")],
    pathex=[HERE],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Stdlib dead weight (never imported by the app or its deps).
        "tkinter",
        "unittest",
        "pydoc",
        "doctest",
        "test",
        # Non-Windows desktop shells (frozen app targets winforms only).
        "webview.platforms.gtk",
        "webview.platforms.qt",
        "webview.platforms.cocoa",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "gi",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="YTQuickie",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[
        # Already-compressed or signature-sensitive binaries: packing them
        # saves nothing and can trip antivirus heuristics.
        "*.dll",
        "python*.dll",
        "pywin32*",
        "win32*",
    ],
    name="YTQuickie",
)