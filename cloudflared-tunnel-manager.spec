# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_all

APP_NAME = os.environ.get("APP_NAME", "cloudflared-tunnel-manager")

gi_datas, gi_binaries, gi_hiddenimports = collect_all("gi")

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=gi_binaries,
    datas=[("data", "data"), *gi_datas],
    hiddenimports=[
        *gi_hiddenimports,
        "gi.repository.Gtk",
        "gi.repository.Gdk",
        "gi.repository.Gsk",
        "gi.repository.Graphene",
        "gi.repository.Adw",
        "gi.repository.Gio",
        "gi.repository.GLib",
        "gi.repository.GObject",
        "gi.repository.GdkPixbuf",
        "gi.repository.Pango",
        "gi.repository.PangoCairo",
        "gi.repository.HarfBuzz",
        "gi.repository.cairo",
        "gi._gi_cairo",
    ],
    hookspath=[],
    hooksconfig={
        "gi": {
            "module-versions": {
                "Gtk": "4.0",
                "Gdk": "4.0",
                "Gsk": "4.0",
                "Adw": "1",
            },
            # 只打包必要的图标主题 / 翻译，控制体积
            "icons": ["Adwaita", "hicolor"],
            "themes": ["Default"],
            "languages": ["zh_CN", "zh_TW", "zh_HK", "en_US", "en_GB"],
        },
    },
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "numpy", "matplotlib"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
