"""端口转发对话框：管理本地 / 远端 / 动态三类转发规则。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
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

from ..forwarding import (
    KIND_DYNAMIC,
    KIND_LABELS,
    KIND_LOCAL,
    KIND_REMOTE,
    ForwardManager,
    ForwardRule,
    load_rules,
    save_rules,
)
from ..hosts import Host
from ..sshclient import SSHClient

STATE_ROLE = Qt.ItemDataRole.UserRole

HINT_TEXT = {
    KIND_LOCAL: "本机监听端口，流量经 SSH 送到服务器可达的目标（等价 ssh -L）。",
    KIND_REMOTE: "让服务器监听端口，连接经 SSH 送回本机（等价 ssh -R）。",
    KIND_DYNAMIC: "本机起一个 SOCKS5 代理，目标由客户端在握手时指定（等价 ssh -D）。",
}


class ForwardDialog(QDialog):
    """查看、编辑并启停某台主机的转发规则。"""

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
        self.resize(820, 460)

        self.rules = load_rules(host)

        # 优先复用已打开的会话连接
        self.client = existing_client if (existing_client is not None and existing_client.channel is not None) else None
        self._owns_client = False

        self.manager = ForwardManager(self.client or SSHClient(host))
        self.manager.state_changed.connect(self._on_state_changed)

        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        column = QVBoxLayout(self)

        self.hint = QLabel("")
        self.hint.setStyleSheet("color: #8b949e;")
        self.hint.setWordWrap(True)
        column.addWidget(self.hint)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["名称", "类型", "本机", "远端", "状态"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
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
            self.table.setItem(row, 1, QTableWidgetItem(rule.kind_label))
            self.table.setItem(row, 2, QTableWidgetItem(self._local_text(rule)))
            self.table.setItem(row, 3, QTableWidgetItem(self._remote_text(rule)))
            self.table.setItem(row, 4, QTableWidgetItem("● 运行中" if self.manager.is_active(index) else "○ 已停止"))

        self._sync_buttons()
        self._update_hint()

    @staticmethod
    def _local_text(rule: ForwardRule) -> str:
        if rule.kind == KIND_REMOTE:
            return f"{rule.remote_host}:{rule.local_port}"

        return f"{rule.bind}:{rule.local_port}"

    @staticmethod
    def _remote_text(rule: ForwardRule) -> str:
        if rule.kind == KIND_REMOTE:
            return f"{rule.bind}:{rule.remote_port}"

        if rule.kind == KIND_DYNAMIC:
            return "（由 SOCKS5 客户端决定）"

        return f"{rule.remote_host}:{rule.remote_port}"

    def _selected_index(self) -> int | None:
        rows = {index.row() for index in self.table.selectedIndexes()}

        if len(rows) != 1:
            return None

        item = self.table.item(rows.pop(), 0)

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

        self._update_hint()

    def _update_hint(self) -> None:
        index = self._selected_index()
        kind = self.rules[index].kind if index is not None else KIND_LOCAL
        self.hint.setText(f"{KIND_LABELS[kind]}：{HINT_TEXT[kind]}　通过 {self.host.target} 传输。")

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
        dialog.setMinimumWidth(460)

        form = QFormLayout(dialog)

        label_edit = QLineEdit(rule.label)
        label_edit.setPlaceholderText("可选，例如：内网 Web")
        form.addRow("名称", label_edit)

        kind_combo = QComboBox()

        for kind, text in KIND_LABELS.items():
            kind_combo.addItem(text, kind)

        index = kind_combo.findData(rule.kind)
        kind_combo.setCurrentIndex(max(0, index))
        form.addRow("类型", kind_combo)

        bind_edit = QLineEdit(rule.bind or "127.0.0.1")
        form.addRow("监听地址", bind_edit)

        local_port_label = QLabel("本机端口")
        local_port = QSpinBox()
        local_port.setRange(1, 65535)
        local_port.setValue(int(rule.local_port))
        form.addRow(local_port_label, local_port)

        remote_host_label = QLabel("目标地址")
        remote_host = QLineEdit(rule.remote_host or "127.0.0.1")
        form.addRow(remote_host_label, remote_host)

        remote_port_label = QLabel("目标端口")
        remote_port = QSpinBox()
        remote_port.setRange(1, 65535)
        remote_port.setValue(int(rule.remote_port))
        form.addRow(remote_port_label, remote_port)

        explanation = QLabel("")
        explanation.setWordWrap(True)
        explanation.setStyleSheet("color: #8b949e;")
        form.addRow(explanation)

        def sync_fields() -> None:
            kind = kind_combo.currentData()
            is_remote = kind == KIND_REMOTE
            is_dynamic = kind == KIND_DYNAMIC

            local_port_label.setText("远端监听端口" if is_remote else "本机监听端口")
            remote_host_label.setText("本机目标地址" if is_remote else "目标地址")
            remote_port_label.setText("本机目标端口" if is_remote else "目标端口")

            remote_host_label.setVisible(not is_dynamic)
            remote_host.setVisible(not is_dynamic)
            remote_port_label.setVisible(not is_dynamic)
            remote_port.setVisible(not is_dynamic)

            explanation.setText(HINT_TEXT.get(kind, ""))

        kind_combo.currentIndexChanged.connect(sync_fields)
        sync_fields()

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        box.accepted.connect(dialog.accept)
        box.rejected.connect(dialog.reject)
        form.addRow(box)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None

        return ForwardRule(
            kind=kind_combo.currentData(),
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
        if self.client is not None and self.client.is_connected():
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
