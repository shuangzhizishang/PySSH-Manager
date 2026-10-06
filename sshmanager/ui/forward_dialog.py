"""端口转发对话框：管理主机上的本地端口转发规则。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..forwarding import ForwardManager, ForwardRule, load_rules, save_rules
from ..hosts import Host
from ..sshclient import SSHClient

STATE_ROLE = Qt.ItemDataRole.UserRole


class ForwardDialog(QDialog):
    """查看、编辑并启停某台主机的本地端口转发。"""

    def __init__(
        self,
        host: Host,
        credential: str | None,
        jump: SSHClient | None = None,
        existing_client: SSHClient | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)

        self.host = host
        self.credential = credential
        self.jump = jump

        self.setWindowTitle(f"端口转发 — {host.display_name}")
        self.resize(720, 420)

        self.rules = load_rules(host)

        # 优先复用已有会话的连接，避免再开一条 SSH
        self.client = existing_client if (existing_client is not None and existing_client.channel is not None) else None
        self._owns_client = False

        self.manager = ForwardManager(self.client or SSHClient(host))
        self.manager.state_changed.connect(self._on_state_changed)

        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        column = QVBoxLayout(self)

        hint = QLabel(
            f"通过 {self.host.target} 转发本地端口；流量经 SSH 加密传输，等价于 ssh -L。"
        )
        hint.setStyleSheet("color: #8b949e;")
        hint.setWordWrap(True)
        column.addWidget(hint)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["名称", "本地监听", "远端目标", "状态", "说明"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        column.addWidget(self.table, 1)

        buttons = QHBoxLayout()

        self.add_button = QPushButton("新增规则")
        self.add_button.clicked.connect(self.add_rule)

        self.edit_button = QPushButton("编辑规则")
        self.edit_button.clicked.connect(self.edit_rule)

        self.remove_button = QPushButton("删除规则")
        self.remove_button.clicked.connect(self.remove_rule)

        self.start_button = QPushButton("开启转发")
        self.start_button.clicked.connect(self.start_selected)

        self.stop_button = QPushButton("停止转发")
        self.stop_button.clicked.connect(self.stop_selected)

        self.stop_all_button = QPushButton("全部停止")
        self.stop_all_button.clicked.connect(self.stop_all)

        for button in (
            self.add_button,
            self.edit_button,
            self.remove_button,
            self.start_button,
            self.stop_button,
            self.stop_all_button,
        ):
            buttons.addWidget(button)

        buttons.addStretch()
        column.addLayout(buttons)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        column.addWidget(self.status)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        box.accepted.connect(self.accept)
        column.addWidget(box)

    # ------------------------------------------------------------------

    def refresh(self) -> None:
        self.table.setRowCount(0)

        for index, rule in enumerate(self.rules):
            row = self.table.rowCount()
            self.table.insertRow(row)

            name_item = QTableWidgetItem(rule.label or f"转发 {index + 1}")
            name_item.setData(STATE_ROLE, index)
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, QTableWidgetItem(f"{rule.bind}:{rule.local_port}"))
            self.table.setItem(row, 2, QTableWidgetItem(f"{rule.remote_host}:{rule.remote_port}"))
            self.table.setItem(row, 3, QTableWidgetItem("● 运行中" if self.manager.is_active(index) else "○ 已停止"))
            self.table.setItem(row, 4, QTableWidgetItem(""))

        self._sync_buttons()

    def _selected_index(self) -> int | None:
        rows = {index.row() for index in self.table.selectedIndexes()}

        if len(rows) != 1:
            return None

        row = rows.pop()
        item = self.table.item(row, 0)

        if item is None:
            return None

        return item.data(STATE_ROLE)

    def _sync_buttons(self, *_args) -> None:
        index = self._selected_index()
        has_selection = index is not None
        running = has_selection and self.manager.is_active(index)

        self.edit_button.setEnabled(has_selection and not running)
        self.remove_button.setEnabled(has_selection and not running)
        self.start_button.setEnabled(has_selection and not running)
        self.stop_button.setEnabled(bool(running))
        self.stop_all_button.setEnabled(self.manager.active_count() > 0)

    # ------------------------------------------------------------------
    # 规则编辑
    # ------------------------------------------------------------------

    def add_rule(self) -> None:
        rule = self._edit_rule_dialog(ForwardRule(local_port=self._next_free_port()))

        if rule is None:
            return

        self.rules.append(rule)
        self._persist()
        self.refresh()

    def edit_rule(self) -> None:
        index = self._selected_index()

        if index is None:
            return

        rule = self._edit_rule_dialog(self.rules[index])

        if rule is None:
            return

        self.rules[index] = rule
        self._persist()
        self.refresh()

    def remove_rule(self) -> None:
        index = self._selected_index()

        if index is None:
            return

        rule = self.rules[index]

        if QMessageBox.question(self, "端口转发", f"删除规则「{rule.summary}」？") != QMessageBox.StandardButton.Yes:
            return

        if self.manager.is_active(index):
            self.manager.stop(index)

        self.rules.pop(index)
        self._persist()
        self.refresh()

    def _edit_rule_dialog(self, rule: ForwardRule) -> ForwardRule | None:
        dialog = QDialog(self)
        dialog.setWindowTitle("转发规则")
        dialog.setMinimumWidth(420)

        form = QFormLayout(dialog)

        label_edit = QLineEdit(rule.label)
        label_edit.setPlaceholderText("可选，例如：内网 Web")
        form.addRow("名称", label_edit)

        bind_edit = QLineEdit(rule.bind or "127.0.0.1")
        form.addRow("本地地址", bind_edit)

        local_port = QSpinBox()
        local_port.setRange(1, 65535)
        local_port.setValue(int(rule.local_port))
        form.addRow("本地端口", local_port)

        remote_host = QLineEdit(rule.remote_host or "127.0.0.1")
        form.addRow("远端地址", remote_host)

        remote_port = QSpinBox()
        remote_port.setRange(1, 65535)
        remote_port.setValue(int(rule.remote_port))
        form.addRow("远端端口", remote_port)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        box.accepted.connect(dialog.accept)
        box.rejected.connect(dialog.reject)
        form.addRow(box)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None

        return ForwardRule(
            bind=bind_edit.text().strip() or "127.0.0.1",
            local_port=local_port.value(),
            remote_host=remote_host.text().strip() or "127.0.0.1",
            remote_port=remote_port.value(),
            label=label_edit.text().strip(),
        )

    def _next_free_port(self) -> int:
        used = {rule.local_port for rule in self.rules}
        port = 8080

        while port in used and port < 65535:
            port += 1

        return port

    def _persist(self) -> None:
        save_rules(self.host, self.rules)

    # ------------------------------------------------------------------
    # 启停
    # ------------------------------------------------------------------

    def _ensure_client(self) -> bool:
        if self.client is not None and self.client.client is not None:
            return True

        self.status.setText("正在建立 SSH 连接…")
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        try:
            client = SSHClient(self.host)
            client.connect(
                hostname=self.host.hostname,
                port=self.host.port,
                username=self.host.username,
                password=self.credential if self.host.auth_type == "password" else None,
                key_path=self.host.key_path if self.host.auth_type == "key" else None,
                passphrase=self.credential if self.host.auth_type == "key" else None,
                verify_host_key=self.host.verify_host_key,
                jump=self.jump,
            )
        except Exception as error:  # noqa: BLE001
            QMessageBox.warning(self, "端口转发", f"连接失败：\n{error}")
            self.status.setText(f"连接失败：{error}")
            return False
        finally:
            QGuiApplication.restoreOverrideCursor()

        self.client = client
        self._owns_client = True
        self.manager.ssh = client

        return True

    def start_selected(self) -> None:
        index = self._selected_index()

        if index is None:
            return

        if not self._ensure_client():
            return

        self.manager.start(index, self.rules[index])
        self.refresh()

    def stop_selected(self) -> None:
        index = self._selected_index()

        if index is None:
            return

        self.manager.stop(index)
        self.refresh()

    def stop_all(self) -> None:
        self.manager.stop_all()
        self.refresh()

    def _on_state_changed(self, rule: ForwardRule, ok: bool, message: str) -> None:
        self.status.setText(("✔ " if ok else "✖ ") + message)
        self.status.setStyleSheet(f"color: {'#3fb950' if ok else '#f85149'};")

    # ------------------------------------------------------------------

    def closeEvent(self, event):  # noqa: N802
        self.manager.stop_all()

        if self._owns_client and self.client is not None:
            self.client.close()

        event.accept()
