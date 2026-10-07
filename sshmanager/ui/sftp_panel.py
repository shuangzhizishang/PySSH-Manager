"""SFTP 面板：左本地、右远端，支持双向传输与增删改。"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QDir, QEvent, QModelIndex, Qt, Signal
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
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

from ..fileops import (
    MODE_COPY,
    MODE_CUT,
    SIDE_LOCAL,
    SIDE_REMOTE,
    ClipboardItem,
    FileClipboard,
    UndoEntry,
    UndoStack,
    plan_paste,
)
from ..fileworker import LocalFileWorker
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


class LocalFileModel(QFileSystemModel):
    """本地文件模型：根指向"此电脑"，从而列出全部盘符。

    Qt 在「此电脑」视图下会把驱动器显示名重复到第二列，这里把驱动器行的
    第二列置空，避免出现 ``C:  C:`` 这种重复。
    """

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role == Qt.ItemDataRole.DisplayRole and index.column() == 1 and not index.parent().isValid():
            return ""

        return super().data(index, role)


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

        #: 文件剪贴板（复制/剪切）与撤销栈
        self.clipboard = FileClipboard()
        self.undo_stack = UndoStack()

        #: 当前"活动栏"：复制/粘贴/删除作用于这一栏。
        #: 只靠 focusWidget() 在窗口未激活时拿不到焦点，所以同时记录最后一次交互的栏。
        self._active_side = SIDE_REMOTE

        #: 批次号 → {"created": [...], "moved": [...], "pending": int, "label": str, "cut": bool}
        self._batches: dict[int, dict] = {}
        self._batch_counter = 0

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
        self.worker.batch_result.connect(self._on_batch_result)
        self.worker.start()

        # 本地文件操作走独立线程，避免大目录复制卡住界面
        self.local_worker = LocalFileWorker()
        self.local_worker.task_finished.connect(self._on_task_finished)
        self.local_worker.progress.connect(self._on_progress)
        self.local_worker.batch_result.connect(self._on_batch_result)
        self.local_worker.start()

        self._install_shortcuts()

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

        path_row = QHBoxLayout()

        self.drive_combo = QComboBox()
        self.drive_combo.setMinimumWidth(78)
        self.drive_combo.setToolTip("选择盘符")
        self.drive_combo.currentIndexChanged.connect(self._on_drive_changed)

        up_button = QPushButton("↑")
        up_button.setFixedWidth(32)
        up_button.setToolTip("上级目录")
        up_button.clicked.connect(self.local_go_up)

        home_button = QPushButton("~")
        home_button.setFixedWidth(32)
        home_button.setToolTip("用户主目录")
        home_button.clicked.connect(self.local_go_home)

        self.local_path_edit = QLineEdit()
        self.local_path_edit.setPlaceholderText("本地路径")
        self.local_path_edit.returnPressed.connect(
            lambda: self.set_local_directory(self.local_path_edit.text())
        )

        path_row.addWidget(self.drive_combo)
        path_row.addWidget(up_button)
        path_row.addWidget(home_button)
        path_row.addWidget(self.local_path_edit, 1)
        column.addLayout(path_row)

        self.local_model = LocalFileModel(self)
        # 根设为"此电脑"：Qt 里用**模型默认根索引**（QModelIndex()）表示这一层，
        # 它的子项就是各盘符（C:、D: …）。注意 model.index("") 是取不到的（0 行）。
        self.local_model.setRootPath("")
        self.local_model.setFilter(
            QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot | QDir.Filter.Hidden | QDir.Filter.Drives
        )

        self.local_view = LocalTree(self)
        self.local_view.setModel(self.local_model)
        self.local_view.setRootIndex(QModelIndex())
        self.local_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.local_view.setAlternatingRowColors(True)
        self.local_view.setSortingEnabled(True)
        self.local_view.setAcceptDrops(True)
        self.local_view.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.local_view.setDropIndicatorShown(True)

        # 只保留"名称"列，避免在窄栏里堆列
        for index in range(1, 4):
            self.local_view.setColumnHidden(index, True)

        self.local_view.doubleClicked.connect(self._on_local_activated)
        self.local_view.selectionModel().selectionChanged.connect(self._sync_buttons)
        self.local_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.local_view.customContextMenuRequested.connect(self._show_local_menu)
        self.local_view.installEventFilter(self)
        self.local_view.selectionModel().selectionChanged.connect(
            lambda *_args: self.set_active_side(SIDE_LOCAL)
        )

        column.addWidget(self.local_view, 1)

        self._reload_drives()

        # 默认停在"此电脑"层，直接能看到 C: / D:；也可以点盘符下拉直接进入某个盘
        self.local_path_edit.setPlaceholderText("此电脑（双击盘符进入）")
        self._sync_drive_selection()

        return pane

    # ------------------------------------------------------------------
    # 本地目录导航（支持盘符）
    # ------------------------------------------------------------------

    def _reload_drives(self) -> None:
        """列出本机所有盘符。"""

        drives = []

        for drive in QDir.drives():
            path = drive.absolutePath()

            if path and path not in drives:
                drives.append(path)

        if not drives:
            drives = [QDir.homePath()]

        self.drive_combo.blockSignals(True)
        self.drive_combo.clear()

        for path in drives:
            # Windows 上 absolutePath 形如 "C:/"，显示成 "C:"
            label = path.rstrip("/").rstrip("\\") or path
            self.drive_combo.addItem(label, path)

        self.drive_combo.blockSignals(False)
        self._sync_drive_selection()

    def _local_root_path(self) -> str:
        """当前本地根目录；处于"此电脑"层时返回空串。"""

        index = self.local_view.rootIndex()

        return self.local_model.filePath(index) if index.isValid() else ""

    def _sync_drive_selection(self) -> None:
        """根据当前目录高亮对应盘符；"此电脑"层不高亮。"""

        current = self._local_root_path()
        index = self.drive_combo.findData(current) if current else -1

        if index < 0 and current:
            # 当前目录属于某个盘符（例如 C:/Users/…）时选中它的盘
            for position in range(self.drive_combo.count()):
                drive = self.drive_combo.itemData(position)

                if drive and current.lower().startswith(str(drive).replace("\\", "/").lower()):
                    index = position
                    break

        self.drive_combo.blockSignals(True)
        self.drive_combo.setCurrentIndex(index)
        self.drive_combo.blockSignals(False)

    def _on_drive_changed(self, _index: int) -> None:
        path = self.drive_combo.currentData()

        if path:
            self.set_local_directory(path)

    def set_local_directory(self, path: str, update_path_edit: bool = True) -> bool:
        """把本地面板切到指定目录。

        - 空串 / ``""`` / ``"this-pc"`` → 回到"此电脑"层（列出所有盘符）
        - 支持盘符根（``C:/``）、普通目录、以及文件路径（取其父目录）
        """

        raw = (path or "").strip().strip('"')

        if not raw or raw.lower() in ("this-pc", "此电脑", "computer"):
            self.local_view.setRootIndex(QModelIndex())
            self.local_path_edit.clear()
            self._sync_drive_selection()

            return True

        if os.path.isfile(raw):
            raw = os.path.dirname(raw)

        target = Path(raw)

        if not target.exists():
            self.status_label.setText(f"✖ 本地目录不存在：{raw}")
            return False

        index = self.local_model.index(str(target))

        if not index.isValid():
            self.status_label.setText(f"✖ 无法读取本地目录：{raw}")
            return False

        self.local_view.setRootIndex(index)

        if update_path_edit:
            self.local_path_edit.setText(str(target))

        self._sync_drive_selection()

        return True

    def local_go_up(self) -> None:
        """上级目录；已处于盘符根时回到"此电脑"层。"""

        current = self._local_root_path()

        if not current:
            return

        parent = str(Path(current).parent)

        # 盘符根（C:\ 或 C:/）的 parent 还是自己 → 退到"此电脑"
        if not parent or parent == current:
            self.set_local_directory("")
            return

        self.set_local_directory(parent)

    def local_go_home(self) -> None:
        self.set_local_directory(QDir.homePath())

    def _open_local_path(self, path: Path) -> None:
        """双击本地文件时交给系统默认程序打开。"""

        try:
            os.startfile(str(path))  # noqa: S606 - Windows 打开文件
        except Exception as error:  # noqa: BLE001
            self.status_label.setText(f"✖ 打开失败：{error}")

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
        self.remote_table.installEventFilter(self)
        self.remote_table.itemSelectionChanged.connect(lambda: self.set_active_side(SIDE_REMOTE))

        column.addWidget(self.remote_table, 1)

        return pane

    def _build_bottom(self) -> QWidget:
        bar = QWidget()
        bar.setStyleSheet("background-color: #161b22;")
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 4, 8, 4)

        self.copy_button = QPushButton("复制")
        self.copy_button.setToolTip("复制选中项（Ctrl+C）：本地/远端都支持")
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self.copy_selection)

        self.cut_button = QPushButton("剪切")
        self.cut_button.setToolTip("剪切选中项（Ctrl+X）：粘贴后源文件移入回收站")
        self.cut_button.setEnabled(False)
        self.cut_button.clicked.connect(self.cut_selection)

        self.paste_button = QPushButton("粘贴")
        self.paste_button.setToolTip(
            "粘贴到当前焦点那一栏（Ctrl+V）：\n"
            "本地复制 → 远端 = 上传（Windows→Linux）\n"
            "远端复制 → 本地 = 下载（Linux→Windows）"
        )
        self.paste_button.setEnabled(False)
        self.paste_button.clicked.connect(self.paste_clipboard)

        self.undo_button = QPushButton("撤销")
        self.undo_button.setToolTip("撤销上一次文件操作（Ctrl+Z）")
        self.undo_button.setEnabled(False)
        self.undo_button.clicked.connect(self.undo_last)

        self.upload_button = QPushButton("上传 Windows→Linux")
        self.upload_button.setToolTip("把本地选中项上传到远端当前目录")
        self.upload_button.setEnabled(False)
        self.upload_button.clicked.connect(self.upload_selected)

        self.download_button = QPushButton("下载 Linux→Windows")
        self.download_button.setToolTip("把远端选中项下载到本地当前目录")
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(self.download_selected)

        self.mkdir_button = QPushButton("新建目录")
        self.mkdir_button.setEnabled(False)
        self.mkdir_button.clicked.connect(self.make_directory)

        self.delete_button = QPushButton("删除")
        self.delete_button.setToolTip("移入回收站，可用「撤销」恢复")
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self.delete_selected)

        self.cancel_button = QPushButton("取消传输")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.worker_cancel)

        self.resume_check = QCheckBox("断点续传")
        self.resume_check.setChecked(True)
        self.resume_check.setToolTip("传输中断后重试时只补缺失的部分（下载用 .part 临时文件）")
        self.resume_check.toggled.connect(self._on_resume_toggled)

        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumWidth(200)
        self.progress_bar.setVisible(False)

        for widget in (
            self.copy_button,
            self.cut_button,
            self.paste_button,
            self.undo_button,
        ):
            row.addWidget(widget)

        row.addSpacing(6)

        for widget in (
            self.upload_button,
            self.download_button,
            self.mkdir_button,
            self.delete_button,
            self.cancel_button,
            self.resume_check,
        ):
            row.addWidget(widget)

        row.addStretch()
        row.addWidget(self.progress_bar)

        return bar

    def _install_shortcuts(self) -> None:
        """面板内快捷键（只在 SFTP 面板获得焦点时生效，不与终端冲突）。"""

        bindings = (
            ("Ctrl+C", self.copy_selection),
            ("Ctrl+X", self.cut_selection),
            ("Ctrl+V", self.paste_clipboard),
            ("Ctrl+Z", self.undo_last),
            ("Del", self.delete_selected),
        )

        for sequence, handler in bindings:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(handler)

    def _on_resume_toggled(self, checked: bool) -> None:
        self.worker.resume = bool(checked)

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
        menu.addAction("复制（Ctrl+C）", self.copy_selection)
        menu.addAction("剪切（Ctrl+X）", self.cut_selection)
        menu.addAction("粘贴（Ctrl+V）", self.paste_clipboard)
        menu.addSeparator()
        menu.addAction("下载到本地（Linux→Windows）", self.download_selected)
        menu.addAction("重命名", self.rename_selected)
        menu.addSeparator()
        menu.addAction("删除到回收站（Del）", self.delete_selected)
        menu.addAction("撤销（Ctrl+Z）", self.undo_last)
        menu.exec(self.remote_table.viewport().mapToGlobal(position))

    def _show_local_menu(self, position) -> None:
        menu = QMenu(self)
        paths = self._selected_local_paths()
        has_selection = bool(paths)

        menu.addAction("复制（Ctrl+C）", self.copy_selection).setEnabled(has_selection)
        menu.addAction("剪切（Ctrl+X）", self.cut_selection).setEnabled(has_selection)
        menu.addAction("粘贴（Ctrl+V）", self.paste_clipboard)
        menu.addSeparator()
        menu.addAction("上传到远端（Windows→Linux）", self.upload_selected).setEnabled(has_selection)
        menu.addSeparator()
        menu.addAction("删除到回收站（Del）", self.delete_selected).setEnabled(has_selection)
        menu.addAction("撤销（Ctrl+Z）", self.undo_last)
        menu.exec(self.local_view.viewport().mapToGlobal(position))

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
            self.set_local_directory(str(path))
            return

        # 文件：交给系统默认程序打开
        self._open_local_path(path)

    # ==================================================================
    # 操作
    # ==================================================================

    def upload_selected(self) -> None:
        self.upload_paths(self._selected_local_paths(), self._remote_path)

    def upload_paths(self, paths: list[Path], remote_dir: str, batch: int | None = None) -> int:
        """把本地文件/目录上传到指定远端目录（Windows→Linux），返回任务数。"""

        remote_dir = normalize_remote_path(remote_dir)
        targets = [path for path in paths if path.exists()]

        if not targets:
            return 0

        if batch is None:
            batch = self._start_batch(f"上传 {len(targets)} 项", cut=False)

        for path in targets:
            target = normalize_remote_path(f"{remote_dir}/{path.name}")
            self.worker.request_upload(path, target, is_dir=path.is_dir(), batch=batch)
            self._expect(batch, 1)

        self._start_progress(f"上传中（{len(targets)} 项）")

        return len(targets)

    def download_selected(self) -> None:
        local_dir = Path(self.local_model.filePath(self.local_view.rootIndex()))
        self.download_entries(self._selected_remote(), local_dir)

    def download_entries(self, entries, local_dir: Path, batch: int | None = None) -> int:
        """把远端条目下载到本地目录（Linux→Windows），返回任务数。"""

        local_dir = Path(local_dir)
        items = list(entries)

        if not items:
            return 0

        if batch is None:
            batch = self._start_batch(f"下载 {len(items)} 项", cut=False)

        for entry in items:
            self.worker.request_download(entry.path, local_dir / entry.name, is_dir=entry.is_dir, batch=batch)
            self._expect(batch, 1)

        self._start_progress(f"下载中（{len(items)} 项）")

        return len(items)

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

        current = self._local_root_path()

        # 处于"此电脑"层时没有明确目录，落到用户主目录
        return Path(current) if current else Path(QDir.homePath())

    def handle_drop(self, remote_entries, local_paths, local_dir: Path) -> None:
        """处理拖放：远端条目 → 下载（Linux→Windows）；本地文件 → 上传（Windows→Linux）。

        拖放同样进撤销栈，可以用 Ctrl+Z 撤回。
        """

        if remote_entries:
            count = self.download_entries(remote_entries, local_dir)
            self._set_status(f"● 已连接 · 拖入下载 {count} 项（可撤销）", "#3fb950")

        if local_paths:
            count = self.upload_paths(local_paths, self._remote_path)
            self._set_status(f"● 已连接 · 拖入上传 {count} 项（可撤销）", "#3fb950")

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
        """删除选中项（移入回收站，可撤销）。作用于当前焦点那一栏。"""

        side = self._focused_side()

        if side == SIDE_REMOTE:
            entries = self._selected_remote()

            if not entries:
                return

            names = "、".join(entry.name for entry in entries[:5])
            question = f"把远端 {names} 移入回收站？（可撤销）"
        else:
            paths = self._selected_local_paths()

            if not paths:
                return

            names = "、".join(path.name for path in paths[:5])
            question = f"把本地 {names} 移入回收站？（可撤销）"

        if QMessageBox.question(self, "SFTP", question) != QMessageBox.StandardButton.Yes:
            return

        batch = self._start_batch("删除", cut=False)

        if side == SIDE_REMOTE:
            for entry in entries:
                self.worker.request_trash(entry.path, entry.is_dir, batch)
                self._expect(batch, 1)
        else:
            self.local_worker.submit_trash(paths, batch)
            self._expect(batch, 1)

    # ==================================================================
    # 复制 / 剪切 / 粘贴 / 撤销
    # ==================================================================

    def _focused_side(self) -> str:
        """当前操作作用于哪一栏（焦点优先，其次最后一次交互的栏）。"""

        widget = self.focusWidget()

        while widget is not None:
            if widget is self.remote_table or widget is self.remote_table.viewport():
                return SIDE_REMOTE

            if widget is self.local_view or widget is self.local_view.viewport():
                return SIDE_LOCAL

            widget = widget.parentWidget()

        return self._active_side

    def set_active_side(self, side: str) -> None:
        """显式指定活动栏（供快捷键/按钮/自动化使用）。"""

        self._active_side = SIDE_REMOTE if side == SIDE_REMOTE else SIDE_LOCAL
        self._sync_buttons()

    def eventFilter(self, obj, event):  # noqa: N802 - Qt 命名
        if event.type() == QEvent.Type.FocusIn:
            if obj is self.local_view:
                self._active_side = SIDE_LOCAL
            elif obj is self.remote_table:
                self._active_side = SIDE_REMOTE

        return super().eventFilter(obj, event)

    def _clipboard_items(self, side: str, cut: bool) -> list[ClipboardItem]:
        if side == SIDE_REMOTE:
            return [
                ClipboardItem(side=SIDE_REMOTE, path=entry.path, name=entry.name, is_dir=entry.is_dir)
                for entry in self._selected_remote()
            ]

        return [
            ClipboardItem(side=SIDE_LOCAL, path=str(path), name=path.name, is_dir=path.is_dir())
            for path in self._selected_local_paths()
        ]

    def copy_selection(self) -> None:
        self._set_clipboard(MODE_COPY)

    def cut_selection(self) -> None:
        self._set_clipboard(MODE_CUT)

    def _set_clipboard(self, mode: str) -> None:
        side = self._focused_side()
        items = self._clipboard_items(side, mode == MODE_CUT)

        if not items:
            self._set_status("● 已连接 · 没有选中任何条目", "#d29922")
            return

        self.clipboard.set(mode, items)
        self._sync_buttons()
        self._set_status(f"● 已连接 · {self.clipboard.summary()}", "#3fb950")

    def paste_clipboard(self) -> None:
        """粘贴到当前焦点那一栏的当前目录。"""

        if self.clipboard.empty:
            self._set_status("● 已连接 · 剪贴板为空", "#d29922")
            return

        target_side = self._focused_side()
        items = list(self.clipboard.items)
        cut = self.clipboard.mode == MODE_CUT
        label = f"{self.clipboard.mode_label} {len(items)} 项"
        actions = plan_paste(items, target_side)

        batch = self._start_batch(label, cut=cut, sources=items)
        dispatched = 0

        if target_side == SIDE_REMOTE:
            target_dir = self._remote_path

            for item in items:
                if item.side == SIDE_REMOTE:
                    target = f"{target_dir}/{item.name}"

                    if cut:
                        self.worker.request_move(item.path, target, batch)
                    else:
                        self.worker.request_copy(item.path, target, item.is_dir, batch)
                else:
                    target = f"{target_dir}/{item.name}"
                    self.worker.request_upload(item.path, target, item.is_dir, batch)

                self._expect(batch, 1)
                dispatched += 1
        else:
            target_dir = Path(self.local_model.filePath(self.local_view.rootIndex()))

            for item in items:
                if item.side == SIDE_LOCAL:
                    # 本地内部：交给本地线程批量处理（一次提交，撤销用同一批次）
                    continue

                target = target_dir / item.name
                self.worker.request_download(item.path, target, item.is_dir, batch)

            local_sources = [Path(item.path) for item in items if item.side == SIDE_LOCAL]
            local_target = target_dir if target_dir.exists() else Path(QDir.homePath())

            if local_sources:
                if cut:
                    self.local_worker.submit_move(local_sources, local_target, batch)
                else:
                    self.local_worker.submit_copy(local_sources, local_target, batch)

                self._expect(batch, 1)

            dispatched += len([item for item in items if item.side == SIDE_REMOTE])

            if local_sources:
                dispatched += len(local_sources)

        if dispatched == 0:
            self._finish_batch(batch, ok=False, message="没有可粘贴的内容")
            return

        self._set_status(f"● 已连接 · {label} → {'Linux(远端)' if target_side == SIDE_REMOTE else 'Windows(本地)'}"
                         f"：{'；'.join(actions[:3])}", "#3fb950")
        self._start_progress(f"{label}中")

    # ------------------------------------------------------------------
    # 批次与撤销
    # ------------------------------------------------------------------

    def _start_batch(self, label: str, cut: bool, sources: list[ClipboardItem] | None = None) -> int:
        self._batch_counter += 1
        batch = self._batch_counter
        self._batches[batch] = {
            "created": [],
            "moved": [],
            "pending": 0,
            "label": label,
            "cut": cut,
            "sources": list(sources or []),
            "trashed": False,
            "failed": False,
        }

        return batch

    def _expect(self, batch: int, count: int = 1) -> None:
        info = self._batches.get(batch)

        if info is not None:
            info["pending"] += count

    def _on_batch_result(self, batch: int, created, moved) -> None:
        info = self._batches.get(batch)

        if info is None:
            return

        info["created"].extend(list(created or []))
        info["moved"].extend(list(moved or []))
        info["pending"] = max(0, info["pending"] - 1)

        if info["pending"] > 0:
            return

        if info["failed"]:
            self._finish_batch(batch, ok=False, message="有任务失败")
            return

        # 剪切：等搬运全部成功，再把源移入回收站，并把"源→回收站"记进同一条撤销记录
        if info["cut"] and not info["trashed"]:
            info["trashed"] = True
            count = self._dispatch_cut_trash(batch, info)

            if count:
                self._expect(batch, count)
                return

        self._finish_batch(batch, ok=True)

    def _dispatch_cut_trash(self, batch: int, info: dict) -> int:
        """把剪切的源移入回收站（同批次），返回派发的任务数。"""

        count = 0

        for item in info["sources"]:
            if item.side == SIDE_LOCAL:
                self.local_worker.submit_trash([Path(item.path)], batch)
            else:
                self.worker.request_trash(item.path, item.is_dir, batch)

            count += 1

        return count

    def _finish_batch(self, batch: int, ok: bool, message: str = "") -> None:
        info = self._batches.pop(batch, None)

        if info is None:
            return

        if not ok:
            self._set_status(f"● 已连接 · {info['label']}未完成{('：' + message) if message else ''}", "#f85149")
            self._sync_buttons()
            return

        entry = UndoEntry(label=info["label"], created=list(info["created"]), moved=list(info["moved"]))
        self.undo_stack.push(entry)

        if info["cut"]:
            self.clipboard.clear()

        affected = len(entry.created) + len(entry.moved)
        self._set_status(f"● 已连接 · {info['label']}完成（{affected} 项，可 Ctrl+Z 撤销）", "#3fb950")
        self._sync_buttons()

        # 两侧都可能变化：远端目录与本地目录都刷新一次
        self.refresh_remote()
        self._refresh_local()

    def undo_last(self) -> None:
        """撤销上一次文件操作：先移回被移动的，再删掉新建的。"""

        entry = self.undo_stack.pop()

        if entry is None:
            self._set_status("● 已连接 · 没有可撤销的操作", "#d29922")
            return

        local_moved = [(current, original) for side, original, current in entry.moved if side == SIDE_LOCAL]
        remote_moved = [(current, original) for side, original, current in entry.moved if side == SIDE_REMOTE]
        local_created = [path for side, path in entry.created if side == SIDE_LOCAL]
        remote_created = [path for side, path in entry.created if side == SIDE_REMOTE]

        if local_moved:
            self.local_worker.submit_restore(local_moved, f"撤销：{entry.label}")

        if remote_moved:
            self.worker.request_restore(remote_moved, f"撤销：{entry.label}")

        if local_created:
            self.local_worker.submit_delete([Path(path) for path in local_created])

        for path in remote_created:
            # 文件还是目录由 worker 自己判断，避免把文件当目录删
            self.worker.request_remove_any(path)

        self._set_status(f"● 已连接 · 已撤销「{entry.label}」", "#3fb950")
        self._sync_buttons()

        from PySide6.QtCore import QTimer as _QTimer

        _QTimer.singleShot(900, self.refresh_remote)
        _QTimer.singleShot(900, self._refresh_local)

    def _refresh_local(self) -> None:
        """让本地视图重新读取当前目录。"""

        try:
            root = self.local_view.rootIndex()
            self.local_model.fetchMore(root) if hasattr(self.local_model, "fetchMore") else None
            self.local_view.viewport().update()
        except Exception:
            pass

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
            self._fail_pending_batch()

        # 影响远端列表的操作结束后刷新一次
        if task in (
            "新建目录",
            "删除",
            "删除目录",
            "重命名",
            "上传",
            "上传目录",
            "远端复制",
            "远端移动",
            "删除到回收站",
            "撤销",
        ) or task.startswith("撤销："):
            self.refresh_remote()

        self._sync_buttons()

    def _fail_pending_batch(self) -> None:
        """某个批次任务失败：把最早的待完成批次标记为失败并结算。"""

        for batch in list(self._batches):
            info = self._batches[batch]

            if info["pending"] <= 0:
                continue

            info["failed"] = True
            info["pending"] -= 1

            if info["pending"] <= 0:
                self._finish_batch(batch, ok=False, message="有任务失败")

            return

    def _sync_buttons(self, *_args) -> None:
        local_paths = self._selected_local_paths()
        remote_entries = self._selected_remote()
        running = self.worker.isRunning()
        has_selection = bool(local_paths or remote_entries)

        self.copy_button.setEnabled(has_selection)
        self.cut_button.setEnabled(has_selection and running)
        self.paste_button.setEnabled(not self.clipboard.empty and running)
        self.undo_button.setEnabled(self.undo_stack.can_undo and running)
        self.upload_button.setEnabled(bool(local_paths) and running)
        self.download_button.setEnabled(bool(remote_entries) and running)
        self.delete_button.setEnabled(has_selection and running)

    # ==================================================================

    def close(self) -> None:
        self._closing = True

        self.worker.stop()

        if not self.worker.wait(3000):
            self.worker.terminate()
            self.worker.wait(500)

        self.local_worker.stop()

        if not self.local_worker.wait(3000):
            self.local_worker.terminate()
            self.local_worker.wait(500)
