# PyInstaller spec for MaxOverlay POE2.
# Build with:  pyinstaller maxoverlay.spec   (or run build.bat)
#
# winrt 3.x is a namespace package split across many distributions and loads
# its OCR submodules + native runtime dynamically, so we collect everything
# from the winrt packages and add the dynamically-imported submodules by hand.

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for pkg in ("winrt", "winrt_runtime"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# Submodules we import lazily inside _ocr_async()
hiddenimports += [
    "winrt.windows.media.ocr",
    "winrt.windows.storage",
    "winrt.windows.storage.streams",
    "winrt.windows.graphics.imaging",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.globalization",
    "pystray._win32",
]

a = Analysis(
    ["maxoverlay.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["numpy", "scipy", "matplotlib", "pandas", "tkinter.test"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MaxOverlay-POE2",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    runtime_tmpdir=None,
    console=False,          # windowed: no console pops up (overlay shows status)
    disable_windowed_traceback=False,
    icon=None,              # add an .ico path here later if you want one
)
