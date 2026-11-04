# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build in "one folder" mode (SPEC §4, §16).  Never use one-file mode:
# it unpacks itself into the system temp folder on every start (SPEC §11.2).
#
#   pyinstaller packaging/OfflineRailwayMap.spec --noconfirm
#
# Output: dist/OfflineRailwayMap/  (OfflineRailwayMap.exe + _internal/)

from pathlib import Path

ROOT = Path(SPECPATH).parent  # noqa: F821 - provided by PyInstaller
SRC = ROOT / "src"

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(SRC)],
    datas=[(str(SRC / "offline_railway_map" / "web"), "offline_railway_map/web")],
    hiddenimports=[],
    excludes=["tkinter", "unittest", "pydoc", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OfflineRailwayMap",
    console=False,
    upx=False,
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    name="OfflineRailwayMap",
    upx=False,
)
