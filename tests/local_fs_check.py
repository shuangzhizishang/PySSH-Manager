"""离屏验证本地文件模型：能否列出全部盘符与目录内容（不需要联网）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYSSH_DATA_DIR", str(Path(__file__).resolve().parent.parent / "build" / "tmp-files"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QDir, QTimer
from PySide6.QtWidgets import QApplication, QTreeView

from sshmanager.ui.sftp_panel import LocalFileModel


def main() -> int:
    app = QApplication(sys.argv[:1])

    model = LocalFileModel()
    model.setRootPath("")
    model.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot | QDir.Filter.Hidden | QDir.Filter.Drives)

    view = QTreeView()
    view.setModel(model)
    view.setRootIndex(model.index(""))
    view.resize(400, 600)
    view.show()

    drives = [str(drive.absolutePath()) for drive in QDir.drives()]
    print("QDir.drives():", drives)

    result = {"drives_visible": False, "files_visible": False}
    attempts = {"n": 0}

    project = Path(__file__).resolve().parent.parent

    def check() -> None:
        attempts["n"] += 1

        # 用"模型默认根索引"（QModelIndex()）表示「此电脑」层，这里应当列出盘符
        from PySide6.QtCore import QModelIndex

        default_rows = model.rowCount(QModelIndex())
        default_names = [
            model.fileName(model.index(row, 0, QModelIndex())) for row in range(default_rows)
        ]

        empty_index = model.index("")
        empty_rows = model.rowCount(empty_index) if empty_index.isValid() else 0

        # QFileSystemModel 是**按需异步加载**的：必须把视图挂到该目录（视图会触发
        # fetchMore），子项才会出现。这里模拟面板的行为。
        project_index = model.index(str(project))
        view.setRootIndex(project_index if project_index.isValid() else QModelIndex())
        view_rows = model.rowCount(view.rootIndex()) if view.rootIndex().isValid() else 0
        view_names = [
            model.fileName(model.index(row, 0, view.rootIndex()))
            for row in range(min(view_rows, 6))
        ]

        if attempts["n"] == 1 or attempts["n"] == 8:
            print(f"第 {attempts['n']} 次：默认根 {default_rows} 项 {default_names}；空路径索引 {empty_rows} 项")
            print(f"           视图（项目目录）{view_rows} 项 {view_names}")

        result["drives_visible"] = default_rows > 0 and "C:" in default_names
        result["files_visible"] = view_rows > 0

        if (result["drives_visible"] and result["files_visible"]) or attempts["n"] > 20:
            finish()
            return

        QTimer.singleShot(300, check)

    def finish() -> None:
        print("--- 判定 ---")
        print(f"drives_visible: {'OK' if result['drives_visible'] else 'FAIL'}")
        print(f"files_visible: {'OK' if result['files_visible'] else 'FAIL'}")
        print("LOCAL_FS_OK" if all(result.values()) else "LOCAL_FS_FAILED")
        app.quit()

    QTimer.singleShot(300, check)
    app.exec()

    sys.stdout.flush()
    os._exit(0 if all(result.values()) else 1)


if __name__ == "__main__":
    raise SystemExit(main())
