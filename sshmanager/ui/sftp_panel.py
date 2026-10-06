"""SFTP 面板：左本地、右远端，支持双向传输与增删改。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QDir, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileSystemModel,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ..hosts import Host
from ..sftpcore import RemoteEntry, format_size, normalize_remote_path, parent_remote_path
from ..sftpworker import SftpWorker
from ..sshclient import SSHClient

REMOTE_MIME = "application/x-pyssh-remote"

STATE_ROLE = Qt.ItemDataRole.UserRole


def _remote_entries_from_mime(mime) -> list:
    """从拖放数据里取回远端条目（仅本进程内的拖拽会有这个格式）。"""

    if not mime.hasFormat(REMOTE_MIME):
        return []

    raw = bytes(mime.data(REMOTE_MIME)).decode("utf-8", errors="replace")
    entries = []

    for line in raw.splitlines():
        parts = line.split("\t")

        if len(parts) != 3:
            continue

        path, name, flag = parts
        entries.append(RemoteEntry(name=name, path=path, is_dir=flag == "d"))

    return entries


class RemoteTable(QTableWidget):
    """远端文件表格：可拖出（下载到本地）。"""

    def __init__(self, panel, parent=None) -> None:
        super().__init__(parent)

        self.panel = panel

    def startDrag(self, supported_actions) -> None:  # noqa: N802 - Qt 命名
        entries = self.panel.selected_remote_entries()

        if not entries:
            return

        from PySide6.QtCore import QMimeData
        from PySide6.QtGui import QDrag

        payload = "\n".join(
            f"{entry.path}\t{entry.name}\t{'d' if entry.is_dir else 'f'}" for entry in entries
        )

        mime = QMimeData()
        mime.setData(REMOTE_MIME, payload.encode("utf-8"))

        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)


class LocalTree(QTreeView):
    """本地文件树：接收拖动（上传）；把远端条目拖进来则下载。"""

    def __init__(self, panel, parent=None) -> None:
        super().__init__(parent)

        self.panel = panel

    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls() or event.mimeData().hasFormat(REMOTE_MIME):
            event.acceptProposedAction()
            return

        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls() or event.mimeData().hasFormat(REMOTE_MIME):
            event.acceptProposedAction()
            return

        super().dragMoveEvent(event)

    def dropEvent(self, event):  # noqa: N802
        remote_entries = _remote_entries_from_mime(event.mimeData())
        local_paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        target = self.panel.local_directory_for_drop(self.indexAt(event.position().toPoint()))

        if remote_entries or local_paths:
            self.panel.handle_drop(remote_entries, local_paths, target)
            event.acceptProposedAction()
            return

        super().dropEvent(event)


class SftpPanel(QWidget):
    """一个主机对应一个 SFTP 标签页。"""

    title_changed = Signal(str)
    closed = Signal()

    def __init__(
        self,
        host: Host,
        credential: str | None = None,
        jump: SSHClient | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)

        self.host = host
        self.credential = credential
        self._closing = False
        self._remote_entries: list[RemoteEntry] = []
        self._remote_path = "/"
        self._home = "/"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_status_bar())
        layout.addWidget(self._build_center(), 1)
        layout.addWidget(self._build_bottom())

        self.worker = SftpWorker(host, credential=credential, jump=jump)
        self.worker.connecting.connect(self._on_connecting)
        self.worker.connected.connect(self._on_connected)
        self.worker.failed.connect(self._on_failed)
        self.worker.disconnected.connect(self._on_disconnected)
        self.worker.listing.connect(self._on_listing)
        self.worker.task_finished.connect(self._on_task_finished)
        self.worker.progress.connect(self._on_progress)
        self.worker.start()

    # ==================================================================
    # 界面构建
    # ==================================================================

    def _build_status_bar(self) -> QWidget:
        bar = QWidget()
        bar.setStyleSheet("background-color: #161b22;")
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 4, 8, 4)

        self.status_label = QLabel("○ 正在建立 SFTP 连接...")
        self.status_label.setStyleSheet("color: #d29922;")

        self.target_label = QLabel(self.host.target)
        self.target_label.setStyleSheet("color: #8b949e;")

        row.addWidget(self.status_label)
        row.addStretch()
        row.addWidget(self.target_label)

        return bar

    def _build_center(self) -> QWidget:
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_local_pane())
        splitter.addWidget(self._build_remote_pane())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([520, 520])

        return splitter

    def _build_local_pane(self) -> QWidget:
        pane = QWidget()
        column = QVBoxLayout(pane)
        column.setContentsMargins(6, 6, 3, 6)

        header = QLabel("本地")
        header.setStyleSheet("font-weight: bold; padding: 2px;")
        column.addWidget(header)

        self.local_model = QFileSystemModel(self)
        self.local_model.setRootPath(QDir.homePath())
        self.local_model.setFilter(
            QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot | QDir.Filter.Hidden
        )

        self.local_view = LocalTree(self)
        self.local_view.setModel(self.local_model)
        self.local_view.setRootIndex(self.local_model.index(QDir.homePath()))
        self.local_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.local_view.setAlternatingRowColors(True)
        self.local_view.setSortingEnabled(True)
        self.local_view.setAcceptDrops(True)
        self.local_view.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.local_view.setDropIndicatorShown(True)

        for index in range(1, 4):
            self.local_view.setColumnHidden(index, True)

        self.local_view.doubleClicked.connect(self._on_local_activated)
        self.local_view.selectionModel().selectionChanged.connect(self._sync_buttons)

        column.addWidget(self.local_view, 1)

        return pane

    def _build_remote_pane(self) -> QWidget:
        pane = QWidget()
        column = QVBoxLayout(pane)
        column.setContentsMargins(3, 6, 6, 6)

        header = QLabel("远端")
        header.setStyleSheet("font-weight: bold; padding: 2px;")
        column.addWidget(header)

        path_row = QHBoxLayout()
        up_button = QPushButton("↑")
        up_button.setFixedWidth(32)
        up_button.setToolTip("上级目录")
        up_button.clicked.connect(self.go_up)

        home_button = QPushButton("~")
        home_button.setFixedWidth(32)
        home_button.setToolTip("家目录")
        home_button.clicked.connect(self.go_home)

        refresh_button = QPushButton("刷新")
        refresh_button.clicked.connect(self.refresh_remote)

        self.remote_path_edit = QLineEdit("/")
        self.remote_path_edit.returnPressed.connect(
            lambda: self.refresh_remote(self.remote_path_edit.text())
        )

        path_row.addWidget(up_button)
        path_row.addWidget(home_button)
        path_row.addWidget(self.remote_path_edit, 1)
        path_row.addWidget(refresh_button)
        column.addLayout(path_row)

        self.remote_table = RemoteTable(self)
        self.remote_table.setColumnCount(3)
        self.remote_table.setHorizontalHeaderLabels(["名称", "大小", "修改时间"])
        self.remote_table.verticalHeader().setVisible(False)
        self.remote_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.remote_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.remote_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.remote_table.setAlternatingRowColors(True)
        self.remote_table.setDragEnabled(True)
        self.remote_table.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.remote_table.setAcceptDrops(True)
        self.remote_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.remote_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.remote_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.remote_table.doubleClicked.connect(self._on_remote_activated)
        self.remote_table.itemSelectionChanged.connect(self._sync_buttons)
        self.remote_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.remote_table.customContextMenuRequested.connect(self._show_remote_menu)

        column.addWidget(self.remote_table, 1)

        return pane

    def _build_bottom(self) -> QWidget:
        bar = QWidget()
        bar.setStyleSheet("background-color: #161b22;")
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 4, 8, 4)

        self.upload_button = QPushButton("上传 →")
        self.upload_button.setEnabled(False)
        self.upload_button.clicked.connect(self.upload_selected)

        self.download_button = QPushButton("← 下载")
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(self.download_selected)

        self.mkdir_button = QPushButton("新建目录")
        self.mkdir_button.setEnabled(False)
        self.mkdir_button.clicked.connect(self.make_directory)

        self.delete_button = QPushButton("删除")
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self.delete_selected)

        self.cancel_button = QPushButton("取消传输")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.worker_cancel)

        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumWidth(220)
        self.progress_bar.setVisible(False)

        row.addWidget(self.upload_button)
        row.addWidget(self.download_button)
        row.addWidget(self.mkdir_button)
        row.addWidget(self.delete_button)
        row.addWidget(self.cancel_button)
        row.addStretch()
        row.addWidget(self.progress_bar)

        return bar

    # ==================================================================
    # 连接状态
    # ==================================================================

    def _on_connecting(self) -> None:
        self._set_status("○ 正在建立 SFTP 连接...", "#d29922")

    def _on_connected(self, home: str) -> None:
        self._set_status("● 已连接", "#3fb950")
        self._home = normalize_remote_path(home)
        self._remote_path = self._home
        self.remote_path_edit.setText(self._remote_path)
        self.mkdir_button.setEnabled(True)
        self.refresh_remote()
        self.title_changed.emit(self.host.display_name)

    def _on_failed(self, message: str) -> None:
        self._set_status("● 连接失败", "#f85149")
        QMessageBox.warning(self, "SFTP", f"{self.host.display_name} 连接失败：\n{message}")

    def _on_disconnected(self) -> None:
        if self._closing:
            return

        self._set_status("○ 已断开", "#8b949e")
        self.upload_button.setEnabled(False)
        self.download_button.setEnabled(False)
        self.mkdir_button.setEnabled(False)
        self.delete_button.setEnabled(False)

    def _set_status(self, text: str, color: str) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color};")

    # ==================================================================
    # 远端目录
    # ==================================================================

    def refresh_remote(self, path: str | None = None) -> None:
        target = normalize_remote_path(path if path is not None else self.remote_path_edit.text() or self._remote_path)
        self.remote_path_edit.setText(target)
        self.worker.request_list(target)

    def go_up(self) -> None:
        self.refresh_remote(parent_remote_path(self._remote_path))

    def go_home(self) -> None:
        self.refresh_remote(self._home)

    def _on_listing(self, path: str, entries) -> None:
        self._remote_path = normalize_remote_path(path)
        self.remote_path_edit.setText(self._remote_path)
        self._remote_entries = list(entries)

        self.remote_table.setRowCount(0)

        for entry in self._remote_entries:
            row = self.remote_table.rowCount()
            self.remote_table.insertRow(row)

            name_item = QTableWidgetItem(("📁 " if entry.is_dir else "📄 ") + entry.name)
            name_item.setData(Qt.ItemDataRole.UserRole, entry.name)
            name_item.setToolTip(entry.path)

            self.remote_table.setItem(row, 0, name_item)
            self.remote_table.setItem(row, 1, QTableWidgetItem(entry.size_text))
            self.remote_table.setItem(row, 2, QTableWidgetItem(entry.modified_text))

        self._set_status(f"● 已连接 · {len(self._remote_entries)} 项", "#3fb950")
        self._sync_buttons()

    def _selected_remote(self) -> list[RemoteEntry]:
        rows = sorted({index.row() for index in self.remote_table.selectedIndexes()})
        result = []

        for row in rows:
            if 0 <= row < len(self._remote_entries):
                result.append(self._remote_entries[row])

        return result

    def _on_remote_activated(self, index) -> None:
        row = index.row()

        if not (0 <= row < len(self._remote_entries)):
            return

        entry = self._remote_entries[row]

        if entry.is_dir:
            self.refresh_remote(entry.path)

    def _show_remote_menu(self, position) -> None:
        entries = self._selected_remote()

        if not entries:
            return

        menu = QMenu(self)
        menu.addAction("下载到本地", self.download_selected)
        menu.addAction("重命名", self.rename_selected)
        menu.addSeparator()
        menu.addAction("删除", self.delete_selected)
        menu.exec(self.remote_table.viewport().mapToGlobal(position))

    # ==================================================================
    # 本地目录
    # ==================================================================

    def _selected_local_paths(self) -> list[Path]:
        paths = []

        for index in self.local_view.selectionModel().selectedRows(0):
            path = self.local_model.filePath(index)

            if path:
                paths.append(Path(path))

        return paths

    def _on_local_activated(self, index) -> None:
        path = Path(self.local_model.filePath(index))

        if path.is_dir():
            self.local_view.setRootIndex(self.local_model.index(str(path)))

    # ==================================================================
    # 操作
    # ==================================================================

    def upload_selected(self) -> None:
        self.upload_paths(self._selected_local_paths(), self._remote_path)

    def upload_paths(self, paths: list[Path], remote_dir: str) -> int:
        """把本地文件/目录上传到指定远端目录，返回任务数。"""

        remote_dir = normalize_remote_path(remote_dir)
        count = 0

        for path in paths:
            if not path.exists():
                continue

            target = normalize_remote_path(f"{remote_dir}/{path.name}")
            self.worker.request_upload(path, target, is_dir=path.is_dir())
            count += 1

        if count:
            self._start_progress(f"上传中（{count} 项）")

        return count

    def download_selected(self) -> None:
        local_dir = Path(self.local_model.filePath(self.local_view.rootIndex()))
        self.download_entries(self._selected_remote(), local_dir)

    def download_entries(self, entries, local_dir: Path) -> int:
        """把远端条目下载到本地目录，返回任务数。"""

        local_dir = Path(local_dir)
        count = 0

        for entry in entries:
            self.worker.request_download(entry.path, local_dir / entry.name, is_dir=entry.is_dir)
            count += 1

        if count:
            self._start_progress(f"下载中（{count} 项）")

        return count

    # ------------------------------------------------------------------
    # 拖放
    # ------------------------------------------------------------------

    def selected_remote_entries(self) -> list[RemoteEntry]:
        """供拖拽使用：当前选中的远端条目。"""

        return self._selected_remote()

    def local_directory_for_drop(self, index) -> Path:
        """拖动落点对应的本地目录（落在目录上就进该目录）。"""

        if index is not None and index.isValid():
            path = Path(self.local_model.filePath(index))

            if path.is_dir():
                return path

            return path.parent

        return Path(self.local_model.filePath(self.local_view.rootIndex()))

    def handle_drop(self, remote_entries, local_paths, local_dir: Path) -> None:
        """处理拖放：远端条目 → 下载；本地文件 → 上传。"""

        if remote_entries:
            count = self.download_entries(remote_entries, local_dir)
            self._set_status(f"● 已连接 · 拖入下载 {count} 项", "#3fb950")

        if local_paths:
            count = self.upload_paths(local_paths, self._remote_path)
            self._set_status(f"● 已连接 · 拖入上传 {count} 项", "#3fb950")

    def make_directory(self) -> None:
        name, ok = QInputDialog.getText(self, "新建目录", "目录名：")

        if not ok or not name.strip():
            return

        self.worker.request_mkdir(f"{self._remote_path}/{name.strip()}")

    def rename_selected(self) -> None:
        entries = self._selected_remote()

        if len(entries) != 1:
            QMessageBox.information(self, "SFTP", "请只选择一个条目。")
            return

        entry = entries[0]
        name, ok = QInputDialog.getText(self, "重命名", "新名称：", text=entry.name)

        if not ok or not name.strip() or name.strip() == entry.name:
            return

        self.worker.request_rename(entry.path, f"{self._remote_path}/{name.strip()}")

    def delete_selected(self) -> None:
        entries = self._selected_remote()

        if not entries:
            return

        names = "、".join(entry.name for entry in entries[:5])

        if QMessageBox.question(self, "SFTP", f"确认删除 {names}？") != QMessageBox.StandardButton.Yes:
            return

        for entry in entries:
            self.worker.request_remove(entry.path, entry.is_dir)

    def worker_cancel(self) -> None:
        self.worker.cancel_current()
        self.cancel_button.setEnabled(False)

    # ==================================================================
    # 进度与结果
    # ==================================================================

    def _start_progress(self, label: str) -> None:
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat(label)
        self.cancel_button.setEnabled(True)

    def _on_progress(self, task: str, done: int, total: int) -> None:
        self.progress_bar.setVisible(True)
        self.progress_bar.setFormat(f"{task} %p%")

        if total > 0:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(int(done * 100 / total))
        else:
            self.progress_bar.setRange(0, 0)

    def _on_task_finished(self, task: str, success: bool, message: str) -> None:
        self.progress_bar.setVisible(False)
        self.progress_bar.setRange(0, 100)
        self.cancel_button.setEnabled(False)

        if success:
            self._set_status(f"● 已连接 · {task}完成", "#3fb950")
        else:
            self._set_status(f"● 已连接 · {task}失败", "#f85149")
            QMessageBox.warning(self, task, message)

        if task in ("新建目录", "删除", "删除目录", "重命名", "上传", "上传目录"):
            self.refresh_remote()

        self._sync_buttons()

    def _sync_buttons(self, *_args) -> None:
        local_paths = self._selected_local_paths()
        remote_entries = self._selected_remote()

        self.upload_button.setEnabled(bool(local_paths) and self.worker.isRunning())
        self.download_button.setEnabled(bool(remote_entries) and self.worker.isRunning())
        self.delete_button.setEnabled(bool(remote_entries) and self.worker.isRunning())

    # ==================================================================

    def close(self) -> None:
        self._closing = True
        self.worker.stop()

        if not self.worker.wait(3000):
            self.worker.terminate()
            self.worker.wait(500)
