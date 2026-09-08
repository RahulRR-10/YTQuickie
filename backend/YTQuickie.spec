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
    excludes=[],
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
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="YTQuickie",
)