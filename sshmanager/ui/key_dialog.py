"""密钥管理对话框：生成、查看公钥、部署到服务器、删除。"""

from __future__ import annotations

import os
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..credentials import CredentialStore
from ..hosts import Host, HostStore
from ..keygen import KEY_TYPES, KeyError_, KeyStore, load_private_key
from ..sshclient import SSHClient

STATE_ROLE = Qt.ItemDataRole.UserRole


class KeyManagerDialog(QDialog):
    """受管理密钥的增删查与部署。"""

    def __init__(
        self,
        key_store: KeyStore,
        host_store: HostStore,
        credentials: CredentialStore,
        clients: dict | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)

        self.keys = key_store
        self.hosts = host_store
        self.credentials = credentials
        #: 主机 id → 已有 SSHClient（复用已打开的会话）
        self.clients = clients if clients is not None else {}

        self.setWindowTitle("SSH 密钥管理")
        self.resize(880, 560)

        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        column = QVBoxLayout(self)

        hint = QLabel(
            "密钥保存在应用的 keys 目录，私钥可用口令加密；部署会把公钥写入远端 ~/.ssh/authorized_keys。"
        )
        hint.setStyleSheet("color: #8b949e;")
        hint.setWordWrap(True)
        column.addWidget(hint)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["名称", "类型", "指纹", "加密", "创建时间"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._on_selection)
        column.addWidget(self.table, 1)

        preview_label = QLabel("公钥内容")
        preview_label.setStyleSheet("font-weight: bold;")
        column.addWidget(preview_label)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFixedHeight(70)
        column.addWidget(self.preview)

        buttons = QHBoxLayout()

        self.generate_button = QPushButton("生成新密钥")
        self.generate_button.clicked.connect(self.generate_key)

        self.copy_button = QPushButton("复制公钥")
        self.copy_button.clicked.connect(self.copy_public)

        self.deploy_button = QPushButton("部署到服务器")
        self.deploy_button.clicked.connect(self.deploy_public)

        self.folder_button = QPushButton("打开密钥目录")
        self.folder_button.clicked.connect(self.open_folder)

        self.delete_button = QPushButton("删除")
        self.delete_button.clicked.connect(self.delete_key)

        for button in (
            self.generate_button,
            self.copy_button,
            self.deploy_button,
            self.folder_button,
            self.delete_button,
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

        for key in self.keys.keys:
            row = self.table.rowCount()
            self.table.insertRow(row)

            name_item = QTableWidgetItem(key.display_name)
            name_item.setData(STATE_ROLE, key.id)

            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, QTableWidgetItem(key.key_type.upper()))
            self.table.setItem(row, 2, QTableWidgetItem(self._fingerprint_text(key)))
            self.table.setItem(row, 3, QTableWidgetItem("是" if key.encrypted else "否"))
            self.table.setItem(
                row,
                4,
                QTableWidgetItem(time.strftime("%Y-%m-%d %H:%M", time.localtime(key.created_at))),
            )

        self._on_selection()

    def _fingerprint_text(self, key) -> str:
        try:
            private_key = load_private_key(key.path, None)
        except Exception:
            return "（加密，需口令）"

        raw = private_key.get_fingerprint().hex()

        return ":".join(raw[index : index + 2] for index in range(0, len(raw), 2))

    def _selected_key(self):
        rows = {index.row() for index in self.table.selectedIndexes()}

        if len(rows) != 1:
            return None

        item = self.table.item(rows.pop(), 0)

        return self.keys.get(item.data(STATE_ROLE)) if item is not None else None

    def _on_selection(self) -> None:
        key = self._selected_key()
        has_key = key is not None

        self.copy_button.setEnabled(has_key)
        self.deploy_button.setEnabled(has_key)
        self.delete_button.setEnabled(has_key)

        if key is None:
            self.preview.setPlainText("")
            return

        try:
            self.preview.setPlainText(self.keys.public_text(key, None))
        except Exception:
            self.preview.setPlainText("（私钥已加密，输入口令后可查看公钥）")

    # ------------------------------------------------------------------

    def generate_key(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("生成新密钥")
        dialog.setMinimumWidth(420)

        form = QFormLayout(dialog)

        name_edit = QLineEdit("id_ed25519")
        form.addRow("名称", name_edit)

        type_combo = QComboBox()

        for key_type in KEY_TYPES:
            type_combo.addItem(key_type.upper(), key_type)

        form.addRow("类型", type_combo)

        bits_spin = QSpinBox()
        bits_spin.setRange(2048, 8192)
        bits_spin.setSingleStep(1024)
        bits_spin.setValue(4096)
        form.addRow("RSA 位数", bits_spin)

        passphrase_edit = QLineEdit()
        passphrase_edit.setEchoMode(QLineEdit.EchoMode.Password)
        passphrase_edit.setPlaceholderText("可留空")
        form.addRow("私钥口令", passphrase_edit)

        comment_edit = QLineEdit("PySSH Manager")
        form.addRow("备注", comment_edit)

        encrypt_check = QCheckBox("用口令加密私钥（推荐）")
        encrypt_check.setChecked(False)
        form.addRow("", encrypt_check)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(dialog.accept)
        box.rejected.connect(dialog.reject)
        form.addRow(box)

        def sync_fields() -> None:
            is_rsa = type_combo.currentData() == "rsa"
            bits_spin.setEnabled(is_rsa)

        type_combo.currentIndexChanged.connect(sync_fields)
        sync_fields()

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        passphrase = passphrase_edit.text() if encrypt_check.isChecked() else ""
        name = name_edit.text().strip() or "id_ed25519"

        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        try:
            key = self.keys.generate(
                name=name,
                key_type=type_combo.currentData(),
                passphrase=passphrase or None,
                bits=bits_spin.value(),
                comment=comment_edit.text().strip(),
            )
        except KeyError_ as error:
            QGuiApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "生成密钥", str(error))
            return
        except Exception as error:  # noqa: BLE001
            QGuiApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "生成密钥", f"生成失败：{error}")
            return
        finally:
            QGuiApplication.restoreOverrideCursor()

        self.refresh()
        self._select_key(key.id)
        self.status.setText(f"✔ 已生成 {key.filename}（{key.key_type.upper()}）")

    def _select_key(self, key_id: str) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)

            if item is not None and item.data(STATE_ROLE) == key_id:
                self.table.selectRow(row)
                return

    def copy_public(self) -> None:
        key = self._selected_key()

        if key is None:
            return

        passphrase = self._ask_passphrase(key)

        if passphrase is None:
            return

        try:
            text = self.keys.public_text(key, passphrase)
        except Exception as error:  # noqa: BLE001
            QMessageBox.warning(self, "复制公钥", f"读取失败：{error}")
            return

        QGuiApplication.clipboard().setText(text)
        self.status.setText("✔ 公钥已复制到剪贴板")

    def _ask_passphrase(self, key):
        """需要口令时询问；不需要则返回空串；用户取消返回 None。"""

        if not key.encrypted:
            return ""

        text, ok = QInputDialog.getText(
            self,
            "私钥口令",
            f"请输入 {key.display_name} 的私钥口令：",
            QInputDialog.EchoMode.Password,
        )

        return text if ok else None

    def deploy_public(self) -> None:
        key = self._selected_key()

        if key is None:
            return

        hosts = self.hosts.sorted_hosts()

        if not hosts:
            QMessageBox.information(self, "部署公钥", "还没有可用的主机。")
            return

        names = [f"{host.display_name}（{host.username}@{host.hostname}）" for host in hosts]
        choice, ok = QInputDialog.getItem(self, "部署公钥", "选择目标主机：", names, 0, False)

        if not ok:
            return

        host = hosts[names.index(choice)]
        passphrase = self._ask_passphrase(key)

        if passphrase is None:
            return

        try:
            public_text = self.keys.public_text(key, passphrase)
        except Exception as error:  # noqa: BLE001
            QMessageBox.warning(self, "部署公钥", f"读取公钥失败：{error}")
            return

        client = self.clients.get(host.id)
        owns_client = False

        if client is None or not client.is_connected():
            credential = self.credentials.get(host.id)

            if host.auth_type == "password" and not credential:
                credential, ok = QInputDialog.getText(
                    self,
                    "部署公钥",
                    f"请输入 {host.target} 的密码：",
                    QInputDialog.EchoMode.Password,
                )

                if not ok:
                    return

            QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

            try:
                client = SSHClient(host)
                client.connect(
                    hostname=host.hostname,
                    port=host.port,
                    username=host.username,
                    password=credential if host.auth_type == "password" else None,
                    key_path=host.key_path if host.auth_type == "key" else None,
                    passphrase=credential if host.auth_type == "key" else None,
                    verify_host_key=host.verify_host_key,
                )
                owns_client = True
            except Exception as error:  # noqa: BLE001
                QGuiApplication.restoreOverrideCursor()
                QMessageBox.warning(self, "部署公钥", f"连接失败：\n{error}")
                return

        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        try:
            result = self.keys.deploy_public_key(client, key, passphrase)
        except Exception as error:  # noqa: BLE001
            QMessageBox.warning(self, "部署公钥", f"部署失败：\n{error}")
            self.status.setText(f"✖ 部署失败：{error}")
            return
        finally:
            QGuiApplication.restoreOverrideCursor()

            if owns_client:
                client.close()

        self.status.setText(f"✔ 已把 {public_text.split()[0]} 公钥部署到 {host.display_name}：{result}")

    def open_folder(self) -> None:
        from .. import settings

        directory = settings.keys_dir()

        try:
            os.startfile(str(directory))  # noqa: S606 - Windows 打开资源管理器
        except Exception:
            pass

        self.status.setText(f"密钥目录：{directory}")

    def delete_key(self) -> None:
        key = self._selected_key()

        if key is None:
            return

        confirm = QMessageBox.question(
            self,
            "删除密钥",
            f"删除密钥「{key.display_name}」及其私钥/公钥文件？",
        )

        if confirm != QMessageBox.StandardButton.Yes:
            return

        self.keys.remove(key.id)
        self.refresh()
        self.status.setText(f"✔ 已删除 {key.filename}")
