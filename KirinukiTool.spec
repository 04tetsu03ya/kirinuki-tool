# -*- mode: python ; coding: utf-8 -*-
# 切り抜き動画ツール（Windows 用）のアプリ化設定
import re
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_data_files

VERSION = re.search(r'APP_VERSION = "([0-9.]+)"', Path("app.py").read_text(encoding="utf-8")).group(1)

datas, binaries, hiddenimports = [], [], []
for pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "tokenizers"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h
hiddenimports += ["truststore", "certifi", "engine", "transcribe", "updater"]
datas += [("assets", "assets")]
if Path("ffmpeg").exists():
    binaries += [(str(p), "ffmpeg") for p in Path("ffmpeg").glob("*") if p.suffix.lower() in (".exe", ".dll")]

a = Analysis(
    ["app.py"], pathex=["."], binaries=binaries, datas=datas, hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "torch",
              "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore", "PySide6.QtQuick",
              "PySide6.QtQml", "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtPdf"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="KirinukiTool", console=False,
          icon="assets/icon.ico", version=None)
coll = COLLECT(exe, a.binaries, a.datas, name="KirinukiTool")
