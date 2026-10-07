"""命令库对话框：收藏与历史，支持条目级的复制 / 剪切 / 粘贴 / 撤销。

- **收藏**页：按分组筛选、搜索，双击或「发送」把命令发到当前会话；
  条目可以复制/剪切到别的分组（粘贴），删除与粘贴都能撤销（Ctrl+Z）。
- **历史**页：自动记录执行过的命令，可再次发送或一键收藏。
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..commands import MODE_COPY, MODE_CUT, CommandStore, SnippetClipboard

ALL_GROUPS = "（全部）"
NO_GROUP = "（未分组）"


class CommandLibraryDialog(QDialog):
    """命令库：收藏 + 历史。"""

    #: 请求把命令发送到当前会话（Windows→Linux）
    send_requested = Signal(str)

    def __init__(self, store: CommandStore, host_id: str = "", host_label: str = "", parent=None) -> None:
        super().__init__(parent)

        self.store = store
        self.host_id = host_id
        self.clipboard = SnippetClipboard()

        self.setWindowTitle(f"命令库 — {host_label or '全局'}")
        self.resize(880, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_snippets_tab(), "收藏")
        self.tabs.addTab(self._build_history_tab(), "历史")
        layout.addWidget(self.tabs, 1)

        self.status = QLabel("")
        self.status.setStyleSheet("color: #8b949e;")
        layout.addWidget(self.status)

        self._install_shortcuts()
        self.refresh()

    # ==================================================================
    # 界面构建
    # ==================================================================

    def _build_snippets_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(6, 8, 6, 6)

        filters = QHBoxLayout()
        filters.addWidget(QLabel("分组"))
        self.group_filter = QComboBox()
        self.group_filter.setMinimumWidth(140)
        self.group_filter.currentIndexChanged.connect(lambda _index: self.refresh_snippets())
        filters.addWidget(self.group_filter)

        filters.addWidget(QLabel("搜索"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("按命令或备注搜索…")
        self.search.textChanged.connect(lambda _text: self.refresh_snippets())
        filters.addWidget(self.search, 1)

        filters.addWidget(QLabel("粘贴到"))
        self.paste_target = QComboBox()
        self.paste_target.setEditable(True)
        self.paste_target.setMinimumWidth(150)
        self.paste_target.setToolTip("粘贴/剪切后的目标分组，可直接输入新分组名")
        filters.addWidget(self.paste_target)

        layout.addLayout(filters)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["命令", "分组", "备注", "使用次数"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.doubleClicked.connect(lambda _index: self.send_selected())
        layout.addWidget(self.table, 1)

        row = QHBoxLayout()

        buttons = (
            ("发送 → Linux", self.send_selected, "把选中命令发送到当前会话"),
            ("新建", self.new_snippet, "新建一条收藏"),
            ("编辑", self.edit_snippet, "修改选中的收藏"),
            ("复制", lambda: self.copy_selection(MODE_COPY), "复制选中条目（Ctrl+C）"),
            ("剪切", lambda: self.copy_selection(MODE_CUT), "剪切选中条目（Ctrl+X）"),
            ("粘贴", self.paste_clipboard, "把剪贴板里的条目粘贴到目标分组（Ctrl+V）"),
            ("撤销", self.undo, "撤销上一次条目操作（Ctrl+Z）"),
            ("删除", self.delete_selected, "删除选中条目（Del）"),
        )

        for label, handler, tip in buttons:
            button = QPushButton(label)
            button.setToolTip(tip)
            button.clicked.connect(handler)
            row.addWidget(button)

            if label == "发送 → Linux":
                self.send_button = button
            elif label in ("编辑", "复制", "剪切", "粘贴", "删除"):
                pass

        row.addStretch()
        layout.addLayout(row)

        return page

    def _build_history_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(6, 8, 6, 6)

        filters = QHBoxLayout()
        filters.addWidget(QLabel("搜索"))
        self.history_search = QLineEdit()
        self.history_search.setPlaceholderText("按命令内容搜索…")
        self.history_search.textChanged.connect(lambda _text: self.refresh_history())
        filters.addWidget(self.history_search, 1)
        layout.addLayout(filters)

        self.history_table = QTableWidget(0, 2)
        self.history_table.setHorizontalHeaderLabels(["命令", "时间"])
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.verticalHeader().setVisible(False)
        history_header = self.history_table.horizontalHeader()
        history_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        history_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.history_table.doubleClicked.connect(lambda _index: self.send_history())
        layout.addWidget(self.history_table, 1)

        row = QHBoxLayout()

        for label, handler in (
            ("发送 → Linux", self.send_history),
            ("收藏选中", self.favorite_history),
            ("复制", self.copy_history),
            ("刷新", self.refresh_history),
            ("清空历史", self.clear_history),
        ):
            button = QPushButton(label)
            button.clicked.connect(handler)
            row.addWidget(button)

        row.addStretch()
        layout.addLayout(row)

        return page

    def _install_shortcuts(self) -> None:
        bindings = (
            ("Ctrl+C", lambda: self.copy_selection(MODE_COPY)),
            ("Ctrl+X", lambda: self.copy_selection(MODE_CUT)),
            ("Ctrl+V", self.paste_clipboard),
            ("Ctrl+Z", self.undo),
            ("Del", self.delete_selected),
        )

        for sequence, handler in bindings:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(handler)

    # ==================================================================
    # 刷新
    # ==================================================================

    def refresh(self) -> None:
        self.refresh_groups()
        self.refresh_snippets()
        self.refresh_history()

    def refresh_groups(self) -> None:
        current = self.group_filter.currentText()
        groups = self.store.groups()

        self.group_filter.blockSignals(True)
        self.group_filter.clear()
        self.group_filter.addItem(ALL_GROUPS)
        self.group_filter.addItems(groups)
        index = self.group_filter.findText(current)

        if index >= 0:
            self.group_filter.setCurrentIndex(index)

        self.group_filter.blockSignals(False)

        target = self.paste_target.currentText()
        self.paste_target.clear()
        self.paste_target.addItem(NO_GROUP)
        self.paste_target.addItems(groups)

        if target:
            self.paste_target.setCurrentText(target)

    def _selected_group(self) -> str:
        text = self.group_filter.currentText()

        return "" if text == ALL_GROUPS else text

    def _paste_group(self) -> str:
        text = self.paste_target.currentText().strip()

        return "" if text == NO_GROUP else text

    def refresh_snippets(self) -> None:
        group = self._selected_group()
        keyword = self.search.text().strip()
        items = self.store.snippets_for(self.host_id, group=group, keyword=keyword)

        self.table.setRowCount(0)

        for snippet in items:
            row = self.table.rowCount()
            self.table.insertRow(row)

            first = QTableWidgetItem(snippet.display)
            first.setData(Qt.ItemDataRole.UserRole, snippet.id)
            first.setToolTip(snippet.text)
            self.table.setItem(row, 0, first)
            self.table.setItem(row, 1, QTableWidgetItem(snippet.group or NO_GROUP))
            self.table.setItem(row, 2, QTableWidgetItem(snippet.note))
            self.table.setItem(row, 3, QTableWidgetItem(str(snippet.use_count)))

        self._update_buttons()
        self._set_status(f"收藏 {len(items)} 条 · {self.clipboard.summary()}")

    def refresh_history(self) -> None:
        keyword = self.history_search.text().strip()
        entries = self.store.history_for(self.host_id, keyword=keyword)

        self.history_table.setRowCount(0)

        for entry in entries:
            row = self.history_table.rowCount()
            self.history_table.insertRow(row)

            first = QTableWidgetItem(entry.display)
            first.setData(Qt.ItemDataRole.UserRole, entry.text)
            first.setToolTip(entry.text)
            self.history_table.setItem(row, 0, first)
            stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(entry.time))
            self.history_table.setItem(row, 1, QTableWidgetItem(stamp))

    def _set_status(self, text: str) -> None:
        self.status.setText(text)

    def _update_buttons(self) -> None:
        if hasattr(self, "send_button"):
            self.send_button.setEnabled(bool(self.selected_snippet_ids()))

    # ==================================================================
    # 选中项
    # ==================================================================

    def selected_snippet_ids(self) -> list[str]:
        ids = []

        for index in self.table.selectionModel().selectedRows(0):
            value = self.table.item(index.row(), 0).data(Qt.ItemDataRole.UserRole)

            if value:
                ids.append(value)

        return ids

    def selected_snippets(self):
        return [item for item in (self.store.get(sid) for sid in self.selected_snippet_ids()) if item is not None]

    # ==================================================================
    # 发送
    # ==================================================================

    def send_selected(self) -> None:
        items = self.selected_snippets()

        if not items:
            return

        for snippet in items:
            self.store.mark_used(snippet.id)

        texts = [snippet.text for snippet in items]
        self.send_requested.emit("\n".join(texts))
        self._set_status(f"已发送 {len(texts)} 条命令")
        self.refresh_snippets()

    def send_history(self) -> None:
        rows = self.history_table.selectionModel().selectedRows(0)

        if not rows:
            return

        texts = [self.history_table.item(index.row(), 0).data(Qt.ItemDataRole.UserRole) for index in rows]
        self.send_requested.emit("\n".join(text for text in texts if text))
        self._set_status(f"已发送 {len(texts)} 条历史命令")

    def copy_history(self) -> None:
        from PySide6.QtGui import QGuiApplication

        rows = self.history_table.selectionModel().selectedRows(0)
        texts = [self.history_table.item(index.row(), 0).data(Qt.ItemDataRole.UserRole) for index in rows]

        if not texts:
            return

        QGuiApplication.clipboard().setText("\n".join(texts))
        self._set_status(f"已复制 {len(texts)} 条历史命令到剪贴板")

    def favorite_history(self) -> None:
        rows = self.history_table.selectionModel().selectedRows(0)
        added = 0

        for index in rows:
            text = self.history_table.item(index.row(), 0).data(Qt.ItemDataRole.UserRole)

            if self.store.add(text, host_id=self.host_id):
                added += 1

        self.refresh()
        self._set_status(f"已收藏 {added} 条（重复的不再添加）")

    def clear_history(self) -> None:
        if QMessageBox.question(self, "命令历史", "清空当前主机的历史记录？") != QMessageBox.StandardButton.Yes:
            return

        removed = self.store.clear_history(self.host_id)
        self.refresh_history()
        self._set_status(f"已清空 {removed} 条历史")

    # ==================================================================
    # 条目编辑
    # ==================================================================

    def new_snippet(self) -> None:
        text, ok = QInputDialog.getMultiLineText(self, "新建命令", "命令内容：")

        if not ok or not text.strip():
            return

        snippet = self.store.add(text, host_id=self.host_id)

        if snippet is None:
            self._set_status("该命令已存在")
            return

        self.refresh()
        self._set_status("已新增一条收藏")

    def edit_snippet(self) -> None:
        items = self.selected_snippets()

        if len(items) != 1:
            self._set_status("请只选择一条收藏再编辑")
            return

        snippet = items[0]
        text, ok = QInputDialog.getMultiLineText(self, "编辑命令", "命令内容：", snippet.text)

        if not ok or not text.strip():
            return

        note, ok = QInputDialog.getText(self, "编辑命令", "备注：", text=snippet.note)

        if not ok:
            return

        group, ok = QInputDialog.getText(self, "编辑命令", "分组：", text=snippet.group)

        if not ok:
            return

        self.store.update(snippet.id, text=text, note=note, group=group)
        self.refresh()
        self._set_status("已更新")

    def delete_selected(self) -> None:
        ids = self.selected_snippet_ids()

        if not ids:
            return

        if QMessageBox.question(self, "命令库", f"删除选中的 {len(ids)} 条收藏？（可 Ctrl+Z 撤销）") != QMessageBox.StandardButton.Yes:
            return

        removed = self.store.remove(ids)
        self.refresh()
        self._set_status(f"已删除 {removed} 条（可 Ctrl+Z 撤销）")

    # ==================================================================
    # 复制 / 剪切 / 粘贴 / 撤销
    # ==================================================================

    def copy_selection(self, mode: str) -> None:
        items = self.selected_snippets()

        if not items:
            self._set_status("没有选中任何条目")
            return

        self.clipboard.set(mode, items)
        self.refresh_snippets()
        self._set_status(f"{self.clipboard.summary()}（粘贴到「{self._paste_group() or '未分组'}」）")

    def paste_clipboard(self) -> None:
        if self.clipboard.empty:
            self._set_status("剪贴板为空")
            return

        group = self._paste_group()
        count = self.store.paste(self.clipboard, group)

        if self.clipboard.mode == MODE_CUT:
            self.clipboard.clear()

        self.refresh()
        self._set_status(f"已粘贴 {count} 条到「{group or '未分组'}」（可 Ctrl+Z 撤销）")

    def undo(self) -> None:
        label = self.store.undo()

        self.refresh()
        self._set_status(f"已撤销：{label}" if label else "没有可撤销的操作")
