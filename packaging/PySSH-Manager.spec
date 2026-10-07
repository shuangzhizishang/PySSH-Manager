# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 PySSH Manager 打成 Windows 可执行程序。

用法（在仓库根目录）::

    .venv\\Scripts\\python.exe -m PyInstaller --noconfirm --clean packaging\\PySSH-Manager.spec

默认产出**目录版**（``dist/PySSH-Manager/PySSH-Manager.exe``，启动快、便于排查）；
设置环境变量 ``PYSSH_ONEFILE=1`` 则产出**单文件版**（``dist/PySSH-Manager.exe``）。

注意：必须在 Windows 上打包才能得到 Windows 程序（跨平台打包不支持）。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all  # noqa: F401 - 备用

PROJECT_ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - SPECPATH 由 PyInstaller 注入
ONEFILE = os.environ.get("PYSSH_ONEFILE", "").strip() not in ("", "0", "false", "False")


def read_version() -> str:
    """从 sshmanager/__init__.py 读版本号，避免两处维护。"""

    text = (PROJECT_ROOT / "sshmanager" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)

    return match.group(1) if match else "0.0.0"


VERSION = read_version()
ICON = PROJECT_ROOT / "packaging" / "icon.ico"


def collect_winpty():
    """把 pywinpty 的文件按原布局打进包里的 ``winpty/`` 目录。

    ``collect_all`` 会把二进制摊到根目录，而 pywinpty 的扩展模块是按"与
    ``_winpty.pyd`` 同目录"去找 ``conpty.dll`` / ``OpenConsole.exe`` 的，
    摊平后 ConPTY 就起不来（表现为本地终端读不到任何输出）。所以这里显式
    按 ``winpty/`` 目标目录收集，保持和 site-packages 一样的结构。
    """

    import winpty as winpty_pkg

    package_dir = Path(winpty_pkg.__file__).parent

    binaries = []
    datas = []
    hidden = ["winpty", "winpty.ptyprocess", "winpty.enums", "winpty._winpty"]

    for item in sorted(package_dir.iterdir()):
        if not item.is_file():
            continue

        suffix = item.suffix.lower()

        if suffix in (".dll", ".pyd"):
            binaries.append((str(item), "winpty"))
        elif suffix in (".exe", ""):
            datas.append((str(item), "winpty"))

    return datas, binaries, hidden


winpty_datas, winpty_binaries, winpty_hidden = collect_winpty()

hiddenimports = [*winpty_hidden, "paramiko", "pyte", "argon2"]

# 只用到 QtCore/QtGui/QtWidgets，其余 Qt 模块（尤其 WebEngine/Quick）全部排除
excludes = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtSpatialAudio",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtSerialPort",
    "PySide6.QtSensors", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
    "PySide6.QtTextToSpeech", "PySide6.QtWebView",
    "tkinter", "unittest", "pydoc", "doctest",
]

# exe 的版本信息（右键属性里能看到）
version_info = None

if sys.platform == "win32":
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    parts = [int(part) for part in re.findall(r"\d+", VERSION)[:4]
             ] + [0, 0, 0, 0]
    version_tuple = tuple(parts[:4])
    version_info = VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=version_tuple,
            prodvers=version_tuple,
            mask=0x3F,
            flags=0x0,
            OS=0x40004,
            fileType=0x1,
            subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "080404b0",
                        [
                            StringStruct("CompanyName", "PySSH Manager"),
                            StringStruct("FileDescription", "PySSH Manager — SSH / SFTP 客户端"),
                            StringStruct("FileVersion", VERSION),
                            StringStruct("InternalName", "PySSH-Manager"),
                            StringStruct("OriginalFilename", "PySSH-Manager.exe"),
                            StringStruct("ProductName", "PySSH Manager"),
                            StringStruct("ProductVersion", VERSION),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [2052, 1200])]),
        ],
    )

a = Analysis(
    [str(PROJECT_ROOT / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=winpty_binaries,
    datas=winpty_datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="PySSH-Manager",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        runtime_tmpdir=None,
        console=False,           # GUI 程序：不弹黑窗
        disable_windowed_traceback=False,
        icon=str(ICON) if ICON.exists() else None,
        version=version_info,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="PySSH-Manager",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        icon=str(ICON) if ICON.exists() else None,
        version=version_info,
    )

    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="PySSH-Manager",
    )
